import os
from collections.abc import Iterator

import psycopg
import pytest


@pytest.fixture(autouse=True)
def _clean_audit_rows(request: pytest.FixtureRequest) -> Iterator[None]:
    """The orchestrator picks its own run ids, so rows are removed by id range instead."""
    if request.node.get_closest_marker("db") is None or "DATABASE_URL" not in os.environ:
        yield
        return
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        row = conn.execute("SELECT coalesce(max(id), 0) FROM tool_audit").fetchone()
    last_id = row[0] if row else 0
    yield
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("DELETE FROM tool_audit WHERE id > %s", (last_id,))
