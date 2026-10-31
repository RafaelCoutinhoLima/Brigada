"""search_reviews: semantic search scoped to the trusted restaurant, honest about downgrades."""

import logging
from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any

import pytest

from brigada.mcp_server import search as search_module
from brigada.mcp_server.db import connect, local_tz
from brigada.mcp_server.models import Review
from brigada.mcp_server.server import build_server
from brigada.rag.embeddings import EmbeddingError, HashingEmbedder
from brigada.rag.indexing import missing_reviews, store_embeddings
from brigada.rag.retrieval import Hit, QueryEmbedding, search
from brigada.runtime.mcp_tools import UNTRUSTED_OUTPUT_TOOLS

from .conftest import scoped_call

pytestmark = pytest.mark.db
EMBEDDER = HashingEmbedder(dim=96)  # a dimension no real index uses


def vector(text: str) -> QueryEmbedding:
    return QueryEmbedding(EMBEDDER.model, tuple(EMBEDDER._one(text)))


@pytest.fixture
def indexed() -> Iterator[None]:
    """Hashing embeddings for every review, removed afterwards (other models' stay)."""
    with connect() as conn:
        rows = missing_reviews(conn, EMBEDDER.model)
        with conn.transaction():
            store_embeddings(conn, EMBEDDER.model, rows, [EMBEDDER._one(r["text"]) for r in rows])
    yield
    with connect() as conn:
        conn.execute("DELETE FROM review_embeddings WHERE model = %s", (EMBEDDER.model,))


class Broken:
    model = "broken"

    def __init__(self, error: Exception | None = None, vector: list[float] | None = None) -> None:
        self.error, self.vector = error, vector

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.vector is not None:
            return [self.vector]
        raise self.error or EmbeddingError("embedding service unavailable")


async def call(embedder: Any, **args: Any) -> dict[str, Any]:
    result = await scoped_call(build_server(embedder), "search_reviews", args)
    assert not result.is_error, result
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def owners(ids: Sequence[int]) -> set[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT restaurant_id FROM reviews WHERE id = ANY(%s)", (list(ids),)
        ).fetchall()
    return {r["restaurant_id"] for r in rows}


@pytest.mark.usefixtures("indexed", "run_id")
async def test_vector_search_finds_the_complaints_of_this_restaurant_only() -> None:
    out = await call(EMBEDDER, restaurant_id=2, query="frio", limit=5)
    assert (out["mode"], out["unindexed"], len(out["items"])) == ("vector", 0, 5)
    assert all("frio" in i["untrusted_text"].lower() for i in out["items"])
    other = await call(EMBEDDER, restaurant_id=1, query="feijoada fria", limit=10)
    assert owners([i["review_id"] for i in other["items"]]) <= {1}


@pytest.mark.usefixtures("indexed", "run_id")
async def test_results_keep_the_relevance_order_and_local_time() -> None:
    out = await call(EMBEDDER, restaurant_id=2, query="pouca porção frio", limit=8)
    with connect() as conn:
        expected = search(
            conn, 2, "pouca porção frio", k=8, mode="vector", embedding=vector("pouca porção frio")
        )
    assert [i["review_id"] for i in out["items"]] == [h.review_id for h in expected]
    offset = datetime.fromisoformat(out["items"][0]["created_at"]).utcoffset()
    assert offset == datetime.now(local_tz()).utcoffset()


@pytest.mark.usefixtures("run_id")
async def test_an_empty_index_falls_back_to_keyword_instead_of_answering_nothing() -> None:
    # Nothing indexed for this model: "vector, no results" would read as "no complaints".
    out = await call(EMBEDDER, restaurant_id=2, query="micro-ondas", limit=3)
    assert out["mode"] == "keyword" and out["unindexed"] > 0
    assert out["items"] and all("micro-ondas" in i["untrusted_text"] for i in out["items"])


@pytest.mark.usefixtures("indexed", "run_id")
async def test_a_review_not_indexed_yet_is_still_found() -> None:
    """Red team A1: a new complaint must not be invisible until someone re-indexes."""
    with connect() as conn, conn.transaction(force_rollback=True):
        conn.execute(
            "INSERT INTO reviews (id, restaurant_id, rating, text, created_at) "
            "VALUES (990101, 1, 1, 'barata viva no hambúrguer, vigilância sanitária', now())"
        )
        found = search_module.search_reviews(
            conn,
            search_module.SearchReviewsInput(restaurant_id=1, query="barata vigilancia", limit=5),
            embedding=vector("barata vigilancia"),
        )
    assert found.mode == "hybrid" and found.unindexed == 1
    assert 990101 in [i.review_id for i in found.items]


@pytest.mark.usefixtures("run_id")
@pytest.mark.parametrize(
    "embedder",
    [
        Broken(),
        Broken(error=RuntimeError("boom sk-secret")),  # not an EmbeddingError
        Broken(vector=[0.0] * 96),  # cosine of a zero vector is NaN: arbitrary order
        Broken(vector=[float("nan")] * 96),
    ],
)
async def test_any_embedding_failure_is_a_reported_keyword_search(embedder: Any) -> None:
    out = await call(embedder, restaurant_id=2, query="micro-ondas", limit=3)
    assert out["mode"] == "keyword" and out["items"]
    assert "sk-secret" not in str(out)


@pytest.mark.usefixtures("run_id")
async def test_a_degraded_search_logs_the_error_class_not_the_query_or_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="brigada.mcp_server.server")
    await call(Broken(error=RuntimeError("boom sk-secret")), restaurant_id=2, query="micro-ondas")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged
    assert "sk-secret" not in logged and "micro-ondas" not in logged


@pytest.mark.usefixtures("run_id")
async def test_a_question_with_no_matching_word_returns_nothing_rather_than_noise() -> None:
    out = await call(Broken(), restaurant_id=2, query="zzzz qqqq", limit=3)
    assert (out["mode"], out["items"]) == ("keyword", [])


@pytest.mark.usefixtures("run_id")
async def test_the_final_fetch_drops_ids_of_another_restaurant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Second barrier: even if search() leaked an id of restaurant 1, restaurant 2 never sees
    its text. It should never fire, so when it does it is logged as an error."""
    with connect() as conn:
        foreign = conn.execute("SELECT id FROM reviews WHERE restaurant_id = 1 LIMIT 1").fetchone()
    assert foreign is not None
    monkeypatch.setattr(
        search_module, "search", lambda *a, **k: [Hit(int(foreign["id"]), 1.0, 1, None)]
    )
    out = await call(Broken(), restaurant_id=2, query="qualquer", limit=3)
    assert out["items"] == []
    errors = [r for r in caplog.records if r.name == search_module.logger.name]
    assert [r.levelno for r in errors] == [logging.ERROR]
    assert "qualquer" not in errors[0].getMessage()


def test_keyword_search_ors_terms_ignores_accents_and_ranks_best_first() -> None:
    with connect() as conn:
        accented = search(conn, 2, "porção pequena gelada", k=10, mode="keyword")
        plain = search(conn, 2, "porcao pequena gelada", k=10, mode="keyword")
        both = search(conn, 2, "pouca porção chegou frio", k=10, mode="keyword")
        texts = {
            r["id"]: r["text"].lower()
            for r in conn.execute("SELECT id, text FROM reviews WHERE restaurant_id = 2")
        }
    assert accented, "OR-ed terms: one matching word is enough"
    assert [h.review_id for h in accented] == [h.review_id for h in plain]
    # The best hit has more of the words than the last one.
    words = ("pouca", "porção", "chegou", "frio")
    assert sum(w in texts[both[0].review_id] for w in words) >= sum(
        w in texts[both[-1].review_id] for w in words
    )
    assert [h.keyword_rank for h in both] == sorted(h.keyword_rank or 0 for h in both)


@pytest.mark.usefixtures("indexed")
def test_hybrid_fuses_both_legs() -> None:
    with connect() as conn:
        hits = search(
            conn, 2, "pouca porção", k=10, mode="hybrid", embedding=vector("pouca porção")
        )
    assert any(h.keyword_rank and h.vector_rank for h in hits)


@pytest.mark.usefixtures("indexed")
def test_vectors_of_another_model_or_dimension_are_never_compared() -> None:
    q = vector("frio")
    with connect() as conn:
        other_model = search(
            conn, 2, "frio", k=3, mode="vector", embedding=QueryEmbedding("other-model", q.vector)
        )
        short = search(
            conn,
            2,
            "frio",
            k=3,
            mode="vector",
            embedding=QueryEmbedding(EMBEDDER.model, q.vector[:64]),
        )
    assert other_model == [] and short == []


def test_incoherent_mode_and_embedding_is_a_bug() -> None:
    with connect() as conn, pytest.raises(ValueError, match="incoherent"):
        search(conn, 2, "frio", k=3, mode="vector")
    with connect() as conn, pytest.raises(ValueError, match="incoherent"):
        search(conn, 2, "frio", k=3, mode="keyword", embedding=vector("frio"))


def test_a_vector_cannot_be_filed_under_another_restaurant() -> None:
    import psycopg

    with connect() as conn, conn.transaction(force_rollback=True):
        row = conn.execute("SELECT id FROM reviews WHERE restaurant_id = 2 LIMIT 1").fetchone()
        assert row is not None
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO review_embeddings (review_id, restaurant_id, model, embedding) "
                "VALUES (%s, 1, 'x', '[1]')",
                (row["id"],),
            )


def test_missing_reviews_can_be_limited_to_one_restaurant() -> None:
    with connect() as conn:
        rows = missing_reviews(conn, "never-indexed-model", restaurant_id=1)
    assert rows and {r["restaurant_id"] for r in rows} == {1}


def test_every_tool_that_returns_customer_text_taints_the_run() -> None:
    """Fail closed for the next tool: returning Review (untrusted_text) means it must be in
    UNTRUSTED_OUTPUT_TOOLS, or a write could follow text a customer wrote."""
    returns_reviews = {
        t.name
        for t in build_server()._tool_manager.list_tools()
        if "untrusted_text" in str(t.output_schema or {})
    }
    assert returns_reviews == {"reviews_for_item", "search_reviews"}
    assert returns_reviews <= UNTRUSTED_OUTPUT_TOOLS
    assert Review.model_fields["untrusted_text"]
