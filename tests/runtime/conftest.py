import os
from collections.abc import Iterator

import psycopg
import pytest


@pytest.fixture(autouse=True)
def _clean_test_audit_rows(request: pytest.FixtureRequest) -> Iterator[None]:
    """DB tests here tag their runs "test-..."; remove what they leave in the shared database."""
    yield
    if request.node.get_closest_marker("db") is None or "DATABASE_URL" not in os.environ:
        return
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("DELETE FROM tool_audit WHERE run_id LIKE 'test-%%'")
        conn.execute("DELETE FROM pending_actions WHERE run_id LIKE 'test-%%'")
