"""Keeping review embeddings in step with the reviews (periphery: not on the query path)."""

from collections.abc import Sequence
from typing import Any

from brigada.mcp_server.db import Conn
from brigada.rag.retrieval import to_pgvector


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
    written = 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO review_embeddings (review_id, restaurant_id, model, embedding) "
            "VALUES (%s, %s, %s, %s::vector) ON CONFLICT (review_id, model) DO NOTHING",
            [
                (r["id"], r["restaurant_id"], model, to_pgvector(v))
                for r, v in zip(reviews, vectors, strict=True)
            ],
        )
        written = max(cur.rowcount, 0)
    return written  # ON CONFLICT DO NOTHING: a concurrent indexer's rows are not counted
