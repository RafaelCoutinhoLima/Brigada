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


async def test_real_dataset_restaurant_is_refused_by_postgres_tools() -> None:
    from scripts.register_lakehouse_restaurants import register, to_registry_rows

    rid = 1_999_999
    gold_row = {
        "restaurant_id": rid,
        "source": "delivery_center",
        "name": "PAPA SUCIS",
        "segment": "food",
        "city": "Porto Alegre",
        "state": "RS",
        "completed_orders": 500,
    }
    brigada_row = {**gold_row, "restaurant_id": 1, "source": "brigada"}
    rows = to_registry_rows([gold_row, brigada_row, {**gold_row, "completed_orders": 3}], 100)
    assert [r["id"] for r in rows] == [rid]  # never Brigada's own, never the tiny ones
    url = get_settings().database_url
    register(url, rows)
    try:
        fetch = FakeWarehouse([], date(2021, 1, 1), date(2021, 4, 30), source="delivery_center")
        args = {"restaurant_id": rid, "start_date": "2021-04-01", "end_date": "2021-04-30"}
        async with mcp_toolbox(
            f"lakehouse-{uuid.uuid4().hex}", rid, TOOLS, build_server(lakehouse=_lake(fetch))
        ) as box:
            with pytest.raises(ToolExecutionError, match="lives only in the lakehouse"):
                await box.executor(ToolCall("toolu_1", "sales_by_period", args))
            data = json.loads(await box.executor(ToolCall("toolu_2", "sales_vs_baseline", args)))
            assert data["data_source"] == "delivery_center"
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT data_source, lakehouse_source, city FROM restaurants WHERE id = %s", (rid,)
            ).fetchone()
        assert row == ("lakehouse", "delivery_center", "Porto Alegre/RS")
    finally:
        with psycopg.connect(url) as conn:
            conn.execute("DELETE FROM restaurants WHERE id = %s", (rid,))
