"""Hybrid review search: Portuguese full-text + embeddings, fused with Reciprocal Rank Fusion.

Every query is filtered by the trusted ``restaurant_id`` and, on the vector side, by the
embedding ``model``: vectors from two models live in different spaces and must never be ranked
together. The functions here are synchronous and take a connection; the caller embeds the
query first (embedding is an HTTP call, search is SQL).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from brigada.mcp_server.db import Conn

Mode = Literal["keyword", "vector", "hybrid"]

# RRF constant from Cormack et al. (2009). Larger k flattens the advantage of the top ranks;
# 60 is the published default and works without tuning on lists of a few dozen items.
RRF_K = 60
# How deep each leg looks before fusion. Fusion can only promote what a leg returned.
CANDIDATES = 30


@dataclass(frozen=True)
class Hit:
    review_id: int
    score: float
    keyword_rank: int | None  # 1-based position in the keyword list, None if absent
    vector_rank: int | None


def keyword_ranking(conn: Conn, restaurant_id: int, query: str, limit: int) -> list[int]:
    """Review ids by full-text relevance. Terms are OR-ed: a question in natural language
    rarely has every word in one short review, and AND would return nothing."""
    rows = conn.execute(
        "WITH q AS (SELECT nullif(replace(plainto_tsquery('portuguese', brigada_unaccent(%(q)s))"
        "::text, '&', '|'), '')::tsquery AS tsq) "
        "SELECT r.id FROM reviews r, q WHERE r.restaurant_id = %(rid)s AND q.tsq IS NOT NULL "
        "AND r.search @@ q.tsq ORDER BY ts_rank_cd(r.search, q.tsq) DESC, r.id LIMIT %(n)s",
        {"q": query, "rid": restaurant_id, "n": limit},
    ).fetchall()
    return [int(r["id"]) for r in rows]


def vector_ranking(
    conn: Conn, restaurant_id: int, model: str, query_vector: Sequence[float], limit: int
) -> list[int]:
    """Review ids by cosine distance to the query vector, among this model's vectors."""
    rows = conn.execute(
        "SELECT review_id FROM review_embeddings WHERE restaurant_id = %(rid)s "
        "AND model = %(m)s AND vector_dims(embedding) = %(d)s "
        "ORDER BY embedding <=> %(v)s::vector, review_id LIMIT %(n)s",
        {
            "rid": restaurant_id,
            "m": model,
            "d": len(query_vector),
            "v": to_pgvector(query_vector),
            "n": limit,
        },
    ).fetchall()
    return [int(r["review_id"]) for r in rows]


def rrf(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1 / (k + rank). Uses only ranks,
    so a cosine distance and a ts_rank never have to be put on the same scale."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def search(
    conn: Conn,
    restaurant_id: int,
    query: str,
    *,
    k: int,
    mode: Mode = "hybrid",
    model: str | None = None,
    query_vector: Sequence[float] | None = None,
) -> list[Hit]:
    """Top ``k`` reviews of one restaurant for ``query``."""
    if mode != "keyword" and (model is None or query_vector is None):
        raise ValueError(f"mode {mode!r} needs the embedding model and the query vector")
    kw = keyword_ranking(conn, restaurant_id, query, CANDIDATES) if mode != "vector" else []
    vec = (
        vector_ranking(conn, restaurant_id, model, query_vector, CANDIDATES)  # type: ignore[arg-type]
        if mode != "keyword"
        else []
    )
    kw_pos = {d: i for i, d in enumerate(kw, start=1)}
    vec_pos = {d: i for i, d in enumerate(vec, start=1)}
    fused = rrf([r for r in (kw, vec) if r])
    return [Hit(d, s, kw_pos.get(d), vec_pos.get(d)) for d, s in fused[:k]]


def to_pgvector(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in values) + "]"


def missing_reviews(
    conn: Conn, model: str, *, restaurant_id: int | None = None
) -> list[dict[str, Any]]:
    """Reviews with no embedding for ``model`` yet (id, restaurant_id, text)."""
    return conn.execute(
        "SELECT r.id, r.restaurant_id, r.text FROM reviews r WHERE NOT EXISTS ("
        "  SELECT 1 FROM review_embeddings e WHERE e.review_id = r.id AND e.model = %(m)s) "
        "AND (%(rid)s::int IS NULL OR r.restaurant_id = %(rid)s::int) ORDER BY r.id",
        {"m": model, "rid": restaurant_id},
    ).fetchall()


def store_embeddings(
    conn: Conn, model: str, reviews: Sequence[dict[str, Any]], vectors: Sequence[Sequence[float]]
) -> int:
    """Insert one vector per review (same order as ``missing_reviews`` returned them)."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO review_embeddings (review_id, restaurant_id, model, embedding) "
            "VALUES (%s, %s, %s, %s::vector) ON CONFLICT (review_id, model) DO NOTHING",
            [
                (r["id"], r["restaurant_id"], model, to_pgvector(v))
                for r, v in zip(reviews, vectors, strict=True)
            ],
        )
    return len(reviews)
