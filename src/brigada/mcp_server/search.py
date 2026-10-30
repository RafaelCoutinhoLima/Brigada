"""``search_reviews``: semantic search over one restaurant's reviews (see ``brigada.rag``).

The query is embedded by the tool (an HTTP call) before this runs; here it is only SQL. The
vector leg is the default because it measured best on the labeled queries
(``evals/retrieval.py``); keyword is the explicit, reported fallback when embedding fails.
"""

from collections.abc import Sequence

from brigada.mcp_server.db import Conn, local_tz
from brigada.mcp_server.models import Review, SearchReviewsInput, SearchReviewsResult
from brigada.mcp_server.queries import ensure_restaurant
from brigada.rag.retrieval import search


def search_reviews(
    conn: Conn,
    inp: SearchReviewsInput,
    *,
    model: str | None,
    query_vector: Sequence[float] | None,
) -> SearchReviewsResult:
    ensure_restaurant(conn, inp.restaurant_id)
    mode = "vector" if query_vector is not None else "keyword"
    hits = search(
        conn,
        inp.restaurant_id,
        inp.query,
        k=inp.limit,
        mode=mode,
        model=model,
        query_vector=query_vector,
    )
    ids = [h.review_id for h in hits]
    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT r.id, r.menu_item_id, m.name AS item_name, r.rating, r.text, r.created_at "
            "FROM reviews r LEFT JOIN menu_items m ON m.id = r.menu_item_id "
            "WHERE r.restaurant_id = %s AND r.id = ANY(%s)",
            (inp.restaurant_id, ids),
        ).fetchall()
    }
    tz = local_tz()
    return SearchReviewsResult(
        restaurant_id=inp.restaurant_id,
        query=inp.query,
        mode=mode,
        items=[
            Review(
                review_id=r["id"],
                menu_item_id=r["menu_item_id"],
                menu_item_name=r["item_name"],
                rating=r["rating"],
                created_at=r["created_at"].astimezone(tz),
                untrusted_text=r["text"],
            )
            for r in (rows[i] for i in ids if i in rows)
        ],
    )
