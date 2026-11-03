"""The lakehouse tools through the real MCP path: scope check and audit live in Postgres."""

import json
import uuid
from datetime import date

import psycopg
import pytest

from brigada.config import get_settings
from brigada.mcp_server.server import build_server
from brigada.runtime.mcp_tools import mcp_toolbox
from brigada.runtime.specialists import SALES
from brigada.runtime.tools import ToolExecutionError
from brigada.runtime.types import ToolCall

from .test_queries import DAY, FakeWarehouse, _channel, _lake

pytestmark = pytest.mark.db

TOOLS = (*SALES.tools, *SALES.lakehouse_tools)


async def test_lakehouse_tool_is_scoped_and_audited() -> None:
    fetch = FakeWarehouse([_channel("app", 0.0, 1200.0)], date(2026, 7, 30), DAY)
    run_id = f"lakehouse-{uuid.uuid4().hex}"  # tool_audit is append-only
    args = {"start_date": DAY.isoformat(), "end_date": DAY.isoformat()}
    async with mcp_toolbox(run_id, 1, TOOLS, build_server(lakehouse=_lake(fetch))) as box:
        data = json.loads(
            await box.executor(
                ToolCall("toolu_1", "sales_vs_baseline", {"restaurant_id": 1, **args})
            )
        )
        assert data["days"][0]["channels"][0]["revenue_change_pct"] == -100.0
        with pytest.raises(ToolExecutionError, match="outside this run's scope"):
            await box.executor(
                ToolCall("toolu_2", "sales_vs_baseline", {"restaurant_id": 2, **args})
            )
    assert len(fetch.calls) == 2  # the out-of-scope call never reached the warehouse

    with psycopg.connect(get_settings().database_url) as conn:
        rows = conn.execute(
            "SELECT tool, error IS NULL FROM tool_audit WHERE run_id = %s ORDER BY id", (run_id,)
        ).fetchall()
    assert rows == [("sales_vs_baseline", True), ("sales_vs_baseline", False)]
