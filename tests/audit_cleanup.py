"""Test-only removal of audit rows: tool_audit refuses DELETE unless the transaction opts in."""

from typing import Any

import psycopg


def purge_audit(conn: psycopg.Connection[Any], where: str, params: Any = ()) -> None:
    """``DELETE FROM tool_audit WHERE <where>`` inside a transaction that allows it."""
    with conn.transaction():
        conn.execute("SET LOCAL brigada.audit_maintenance = 'on'")
        conn.execute(f"DELETE FROM tool_audit WHERE {where}", params)  # noqa: S608 - test SQL
