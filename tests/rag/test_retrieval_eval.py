"""The retrieval eval runs end to end offline (hashing embedder), as CI runs it."""

import pytest

from brigada.mcp_server.db import connect
from brigada.rag.embeddings import HashingEmbedder
from evals.retrieval import MODES, QUERIES, evaluate

pytestmark = pytest.mark.db


async def test_eval_scores_every_query_in_every_mode_without_leaking_tenants() -> None:
    embedder = HashingEmbedder(dim=128)
    try:
        with connect() as conn:
            result = await evaluate(conn, embedder)  # asserts no hit from another restaurant
        assert len(result["queries"]) == len(QUERIES)
        assert all(q["relevant"] > 0 for q in result["queries"]), "a query has no relevant review"
        for mode in MODES:
            assert 0 <= result["summary"]["all"][mode]["r_precision"] <= 1
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM review_embeddings WHERE model = %s", (embedder.model,))
