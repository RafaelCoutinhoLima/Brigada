"""``search_reviews``: semantic search over one restaurant's reviews (see ``brigada.rag``).

The query is embedded by the tool (an HTTP call) before this runs; here it is only SQL. The
vector leg is the default because it measured best on the labeled queries
(``evals/retrieval.py``). Every downgrade is reported in ``mode``: hybrid when part of the
corpus has no vector yet, keyword when embedding failed or the index is empty.
"""

import logging

from brigada.mcp_server.db import Conn, local_tz
from brigada.mcp_server.models import Review, SearchReviewsInput, SearchReviewsResult
from brigada.mcp_server.queries import ensure_restaurant
from brigada.rag.retrieval import Mode, QueryEmbedding, coverage, search

logger = logging.getLogger(__name__)


def search_reviews(
    conn: Conn, inp: SearchReviewsInput, *, embedding: QueryEmbedding | None
) -> SearchReviewsResult:
    ensure_restaurant(conn, inp.restaurant_id)
    mode: Mode = "keyword"
    unindexed = 0
    if embedding is not None:
        cov = coverage(conn, inp.restaurant_id, embedding)
        unindexed = cov.unindexed
        if cov.wrong_dimension:
            # The model changed under the same name, or the index was built with another one.
            logger.error(
                "review index: %d vectors of %s have another dimension than the query (%d)",
                cov.wrong_dimension,
                embedding.model,
                len(embedding.vector),
            )
        if cov.indexed == 0:
            # An empty index would answer "no reviews": search by word and say so.
            logger.warning("review index empty for %s, keyword search only", embedding.model)
            embedding = None
        else:
            # Reviews without a vector are invisible to the vector leg; the keyword leg sees
            # them, so an incomplete index searches both.
            mode = "vector" if cov.unindexed == 0 else "hybrid"
    if embedding is None:
        unindexed = conn.execute(
            "SELECT count(*) AS n FROM reviews WHERE restaurant_id = %s", (inp.restaurant_id,)
        ).fetchone()["n"]  # type: ignore[index]
    hits = search(conn, inp.restaurant_id, inp.query, k=inp.limit, mode=mode, embedding=embedding)
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
    if dropped := [i for i in ids if i not in rows]:
        # The second tenant barrier fired: ``search`` returned ids this restaurant does not
        # own (or that vanished meanwhile). It should never happen; ids only, never the query.
        logger.error(
            "search_reviews: restaurant %s got %d review ids it does not own: %s",
            inp.restaurant_id,
            len(dropped),
            dropped,
        )
    tz = local_tz()
    return SearchReviewsResult(
        restaurant_id=inp.restaurant_id,
        query=inp.query,
        mode=mode,
        unindexed=unindexed,
        items=[
            Review(
                review_id=r["id"],
                menu_item_id=r["menu_item_id"],
                menu_item_name=r["item_name"],
                rating=r["rating"],
                created_at=r["created_at"].astimezone(tz),
                untrusted_text=r["text"],
            )
            # Order of relevance, and a second tenant filter: an id of another restaurant
            # that slipped through ``search`` has no row here and is dropped.
            for r in (rows[i] for i in ids if i in rows)
        ],
    )
