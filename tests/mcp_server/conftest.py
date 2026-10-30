"""Fixtures for the MCP server tests.

DB tests use the seeded database (``seed_data.py --reset --end-date 2026-10-27``) and the
0002 migration. They only assert on rows they create and clean those up afterwards, since
other suites (evals) share the same database.
"""

import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from mcp_types import CallToolResult, InputRequiredResult

from brigada.mcp_server.audit import restaurant_context, run_context
from brigada.mcp_server.db import Conn, connect
from brigada.mcp_server.server import BrigadaMCPServer, build_server
from tests.audit_cleanup import purge_audit

END_DATE = date(2026, 10, 27)
START_DATE = date(2026, 7, 30)  # 90-day seed period, inclusive


@pytest.fixture
def server() -> BrigadaMCPServer:
    return build_server()


@pytest.fixture
def db() -> Iterator[Conn]:
    with connect() as conn:
        conn.autocommit = True
        yield conn


@pytest.fixture
def run_id(db: Conn) -> Iterator[str]:
    """A unique run id active for the test; everything tagged with it is removed afterwards.

    Also snapshots ``menu_items.active`` and restores it, so approving a pause in a test
    does not leak into other suites.
    """
    rid = f"test-{uuid.uuid4()}"
    snapshot = db.execute("SELECT id, active FROM menu_items").fetchall()
    with run_context(rid):
        yield rid
    action_ids = [
        r["id"]
        for r in db.execute("SELECT id FROM pending_actions WHERE run_id = %s", (rid,)).fetchall()
    ]
    with db.transaction():
        purge_audit(
            db,
            "run_id = %s OR (tool LIKE 'human.%%' AND (args->>'action_id')::bigint = ANY(%s))",
            (rid, action_ids),
        )
        db.execute("DELETE FROM promotions WHERE source_action_id = ANY(%s)", (action_ids,))
        orders = db.execute(
            "DELETE FROM purchase_orders WHERE source_action_id = ANY(%s) "
            "RETURNING id, movement_id",
            (action_ids,),
        ).fetchall()
        purge_audit(
            db,
            "tool = 'human.receive_purchase' AND (args->>'purchase_order_id')::bigint = ANY(%s)",
            ([o["id"] for o in orders],),
        )
        db.execute(
            "DELETE FROM stock_movements WHERE id = ANY(%s)",
            ([o["movement_id"] for o in orders if o["movement_id"]],),
        )
        db.execute("DELETE FROM pending_actions WHERE id = ANY(%s)", (action_ids,))
        for row in snapshot:
            db.execute(
                "UPDATE menu_items SET active = %s WHERE id = %s AND active IS DISTINCT FROM %s",
                (row["active"], row["id"], row["active"]),
            )


def menu_item_id(db: Conn, restaurant_id: int, name: str) -> int:
    row = db.execute(
        "SELECT id FROM menu_items WHERE restaurant_id = %s AND name = %s", (restaurant_id, name)
    ).fetchone()
    assert row is not None, f"menu item {name!r} not found in restaurant {restaurant_id}"
    return int(row["id"])


async def scoped_call(
    server: BrigadaMCPServer, name: str, arguments: dict[str, Any]
) -> CallToolResult | InputRequiredResult:
    """Call a tool the way the runtime will: scoped to the restaurant in the arguments."""
    rid = arguments.get("restaurant_id")
    # Like the real runtime, the scope is always a trusted int (here: the one asked for, or 1).
    with restaurant_context(rid if type(rid) is int else 1):
        return await server.call_tool(name, arguments)


def audit_rows(db: Conn, run_id: str) -> list[dict[str, object]]:
    return db.execute(
        "SELECT tool, args, result, error, approved_by FROM tool_audit "
        "WHERE run_id = %s ORDER BY id",
        (run_id,),
    ).fetchall()
