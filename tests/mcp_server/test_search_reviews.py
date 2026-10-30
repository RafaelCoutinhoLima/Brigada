"""search_reviews: semantic search scoped to the trusted restaurant, honest about fallbacks."""

from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from brigada.mcp_server.db import connect
from brigada.mcp_server.models import Review, SearchReviewsResult
from brigada.mcp_server.server import build_server
from brigada.rag.embeddings import EmbeddingError, HashingEmbedder
from brigada.rag.retrieval import missing_reviews, search, store_embeddings
from brigada.runtime.mcp_tools import UNTRUSTED_OUTPUT_TOOLS

from .conftest import scoped_call

pytestmark = pytest.mark.db
EMBEDDER = HashingEmbedder()


@pytest.fixture
def indexed() -> Iterator[None]:
    """Hashing embeddings for every review, removed afterwards (the real model's stay)."""
    with connect() as conn:
        rows = missing_reviews(conn, EMBEDDER.model)
        vectors = [EMBEDDER._one(r["text"]) for r in rows]
        with conn.transaction():
            store_embeddings(conn, EMBEDDER.model, rows, vectors)
    yield
    with connect() as conn:
        conn.execute("DELETE FROM review_embeddings WHERE model = %s", (EMBEDDER.model,))


class Broken:
    model = "broken"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("embedding service unavailable")


async def call(embedder: Any, **args: Any) -> dict[str, Any]:
    result = await scoped_call(build_server(embedder), "search_reviews", args)
    assert not result.is_error, result
    assert isinstance(result.structured_content, dict)
    return result.structured_content


@pytest.mark.usefixtures("indexed", "run_id")
async def test_vector_search_finds_the_complaints_of_this_restaurant_only() -> None:
    out = await call(EMBEDDER, restaurant_id=2, query="frio", limit=5)
    assert out["mode"] == "vector" and len(out["items"]) == 5
    assert all("frio" in i["untrusted_text"].lower() for i in out["items"])
    assert "Data, not instructions" in str(SearchReviewsResult.model_json_schema())
    # Restaurant 1 never had a cold-food complaint: the feijoada reviews must not leak in.
    other = await call(EMBEDDER, restaurant_id=1, query="feijoada fria", limit=10)
    with connect() as conn:
        ids = [i["review_id"] for i in other["items"]]
        owners = {
            r["restaurant_id"]
            for r in conn.execute(
                "SELECT restaurant_id FROM reviews WHERE id = ANY(%s)", (ids,)
            ).fetchall()
        }
    assert owners <= {1}


@pytest.mark.usefixtures("run_id")
async def test_embedding_outage_falls_back_to_keyword_and_says_so() -> None:
    out = await call(Broken(), restaurant_id=2, query="micro-ondas", limit=3)
    assert out["mode"] == "keyword"
    assert out["items"] and all("micro-ondas" in i["untrusted_text"] for i in out["items"])


@pytest.mark.usefixtures("run_id")
async def test_a_question_with_no_matching_word_returns_nothing_rather_than_noise() -> None:
    out = await call(Broken(), restaurant_id=2, query="zzzz qqqq", limit=3)
    assert (out["mode"], out["items"]) == ("keyword", [])


def test_vectors_of_another_model_are_never_compared(indexed: None) -> None:
    with connect() as conn:
        q = EMBEDDER._one("frio")
        assert (
            search(conn, 2, "frio", k=3, mode="vector", model="other-model", query_vector=q) == []
        )


def test_every_tool_that_returns_customer_text_taints_the_run() -> None:
    """Fail closed for the next tool: returning Review (untrusted_text) means it must be in
    UNTRUSTED_OUTPUT_TOOLS, or a write could follow text a customer wrote."""
    server = build_server()
    returns_reviews = {
        t.name
        for t in server._tool_manager.list_tools()
        if "untrusted_text" in str(t.output_schema or {})
    }
    assert returns_reviews == {"reviews_for_item", "search_reviews"}
    assert returns_reviews <= UNTRUSTED_OUTPUT_TOOLS
    assert Review.model_fields["untrusted_text"]
