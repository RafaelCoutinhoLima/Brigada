"""Tenant scope: a run bound to one restaurant can't read or write another's data."""

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from brigada.mcp_server.audit import RESTAURANT_ID_META_KEY, RUN_ID_META_KEY, restaurant_context
from brigada.mcp_server.db import Conn
from brigada.mcp_server.server import BrigadaMCPServer, build_server

from .conftest import END_DATE, START_DATE, audit_rows

pytestmark = pytest.mark.db

SALES = {"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat(), "group_by": "none"}


async def test_in_scope_call_is_allowed(server: BrigadaMCPServer, run_id: str) -> None:
    with restaurant_context(3):
        result = await server.call_tool("sales_by_period", {"restaurant_id": 3, **SALES})
    assert result.structured_content is not None


async def test_injected_cross_restaurant_read_is_rejected_and_audited(
    server: BrigadaMCPServer, db: Conn, run_id: str
) -> None:
    # A review in restaurant 3 telling the model to read restaurant 1's sales.
    with restaurant_context(3), pytest.raises(ToolError, match="outside this run's scope"):
        await server.call_tool("sales_by_period", {"restaurant_id": 1, **SALES})
    rows = audit_rows(db, run_id)
    assert [r["tool"] for r in rows] == ["sales_by_period"]
    assert "outside this run's scope" in str(rows[0]["error"])


async def test_cross_restaurant_write_is_rejected(server: BrigadaMCPServer, run_id: str) -> None:
    with restaurant_context(3), pytest.raises(ToolError, match="outside this run's scope"):
        await server.call_tool(
            "pause_menu_item", {"restaurant_id": 1, "menu_item_id": 101, "reason": "injected"}
        )


async def test_scope_via_request_meta(db: Conn, run_id: str) -> None:
    async with Client(build_server()) as client:
        meta = {RUN_ID_META_KEY: run_id, RESTAURANT_ID_META_KEY: 3}
        denied = await client.call_tool("sales_by_period", {"restaurant_id": 1, **SALES}, meta=meta)
        allowed = await client.call_tool(
            "sales_by_period", {"restaurant_id": 3, **SALES}, meta=meta
        )
    assert denied.is_error
    assert not allowed.is_error
