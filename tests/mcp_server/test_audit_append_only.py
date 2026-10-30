"""tool_audit is append-only: what the agents did cannot be edited or erased afterwards."""

import psycopg
import pytest

from brigada.mcp_server import audit
from brigada.mcp_server.db import Conn, connect

pytestmark = pytest.mark.db


@pytest.fixture
def row() -> tuple[Conn, int]:
    with connect() as conn, conn.transaction(force_rollback=True):
        rid = audit.record(conn, tool="test.append_only", args={}, run_id="test-append-only")
        yield conn, rid


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE tool_audit SET error = 'rewritten' WHERE id = %s",
        "DELETE FROM tool_audit WHERE id = %s",
    ],
)
def test_rows_cannot_be_changed_or_deleted(row: tuple[Conn, int], sql: str) -> None:
    conn, rid = row
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        with conn.transaction():
            conn.execute(sql, (rid,))


def test_the_table_cannot_be_truncated(row: tuple[Conn, int]) -> None:
    conn, _ = row
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="TRUNCATE refused"):
        with conn.transaction():
            conn.execute("TRUNCATE tool_audit")


def test_updates_are_refused_even_in_maintenance(row: tuple[Conn, int]) -> None:
    conn, rid = row
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            conn.execute("SET LOCAL brigada.audit_maintenance = 'on'")
            conn.execute("UPDATE tool_audit SET error = 'x' WHERE id = %s", (rid,))


def test_maintenance_allows_deleting_inside_its_own_transaction(row: tuple[Conn, int]) -> None:
    conn, rid = row
    with conn.transaction():
        conn.execute("SET LOCAL brigada.audit_maintenance = 'on'")
        conn.execute("DELETE FROM tool_audit WHERE id = %s", (rid,))
    assert conn.execute("SELECT 1 FROM tool_audit WHERE id = %s", (rid,)).fetchone() is None
