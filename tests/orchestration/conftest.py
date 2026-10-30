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
    tables = ("tool_audit", "pending_actions")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        last = {t: conn.execute(f"SELECT coalesce(max(id), 0) FROM {t}").fetchone() for t in tables}
    yield
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.transaction():
        conn.execute("SET LOCAL brigada.audit_maintenance = 'on'")  # tool_audit is append-only
        for t, row in last.items():
            conn.execute(f"DELETE FROM {t} WHERE id > %s", (row[0] if row else 0,))
