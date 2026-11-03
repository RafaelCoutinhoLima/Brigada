"""Lakehouse tools without Databricks: the fixed queries over a fake warehouse, the server
registration and the specialist wiring. The same SQL runs on real Spark in test_end_to_end."""

from contextlib import contextmanager
from datetime import date
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from brigada.config import Settings
from brigada.lakehouse import queries
from brigada.lakehouse.models import ItemTrendsInput, SalesVsBaselineInput
from brigada.lakehouse.queries import GoldTables
from brigada.lakehouse.warehouse import Lakehouse, from_settings
from brigada.mcp_server.server import build_server
from brigada.runtime import specialists
from brigada.runtime.mcp_tools import stdio_server

TABLES = GoldTables("workspace", "brigada")
DAY = date(2026, 10, 27)


class FakeWarehouse:
    """Answers the coverage query and the main query with canned rows; records every call."""

    def __init__(self, rows: list[dict[str, Any]], data_from: date | None, until: date | None):
        self.rows = rows
        self.coverage = [{"data_from": data_from, "data_until": until}]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        return self.coverage if "max(day) AS data_until" in sql else self.rows


def _channel(channel: str, revenue: float, baseline: float | None, weeks: int = 4) -> dict:
    return {
        "day": DAY,
        "channel": channel,
        "orders": int(revenue // 50),
        "revenue": revenue,
        "baseline_weeks": weeks,
        "baseline_orders": None if baseline is None else baseline / 50,
        "baseline_revenue": baseline,
    }


def test_sales_vs_baseline_totals_the_channels_and_binds_every_value() -> None:
    fetch = FakeWarehouse(
        [_channel("app", 0.0, 1200.0), _channel("balcao", 300.0, 280.0)],
        date(2026, 7, 30),
        DAY,
    )
    inp = SalesVsBaselineInput(restaurant_id=1, start_date=DAY, end_date=DAY)
    result = queries.sales_vs_baseline(fetch, inp, TABLES)

    (day,) = result.days
    assert day.weekday == "tuesday"
    assert [c.channel for c in day.channels] == ["app", "telefone", "balcao"]
    app, telefone, _ = day.channels
    assert app.revenue_change_pct == -100.0
    assert telefone.orders == 0 and telefone.baseline_revenue is None  # missing row: zero
    assert day.revenue == 300.0 and day.baseline_revenue == 1480.0
    assert day.revenue_change_pct == -79.7
    assert result.note is None
    for sql, params in fetch.calls:
        assert "`workspace`.`brigada`.`gold_daily_channel_sales`" in sql
        assert "2026-10-27" not in sql and params["restaurant_id"] == 1  # bound, not inlined


def test_no_history_means_no_baseline_not_zero() -> None:
    fetch = FakeWarehouse([_channel("app", 500.0, None, weeks=0)], DAY, DAY)
    inp = SalesVsBaselineInput(restaurant_id=1, start_date=DAY, end_date=DAY)
    (day,) = queries.sales_vs_baseline(fetch, inp, TABLES).days
    assert day.baseline_weeks == 0
    assert day.baseline_revenue is None and day.revenue_change_pct is None


def test_stale_lakehouse_is_reported_not_hidden() -> None:
    fetch = FakeWarehouse([], date(2026, 7, 30), date(2026, 10, 26))
    inp = SalesVsBaselineInput(restaurant_id=1, start_date=DAY, end_date=DAY)
    result = queries.sales_vs_baseline(fetch, inp, TABLES)
    assert result.days == []
    assert result.note is not None and "2026-10-26" in result.note
    assert "sales_by_period" in result.note

    empty = FakeWarehouse([], None, None)
    assert "no data" in (queries.sales_vs_baseline(empty, inp, TABLES).note or "")


def test_baseline_range_is_capped() -> None:
    with pytest.raises(ValidationError, match="maximum is 31"):
        SalesVsBaselineInput(restaurant_id=1, start_date=date(2026, 9, 1), end_date=DAY)


def _item(item_id: int, current: float, previous: float) -> dict[str, Any]:
    return {
        "menu_item_id": item_id,
        "name": f"Prato {item_id}",
        "category": "principal",
        "active": True,
        "qty_current": int(current // 10),
        "qty_previous": int(previous // 10),
        "revenue_current": current,
        "revenue_previous": previous,
    }


def test_item_trends_windows_and_order() -> None:
    rows = [_item(1, 100.0, 300.0), _item(2, 500.0, 200.0), _item(3, 50.0, 0.0)]
    fetch = FakeWarehouse(rows, date(2026, 7, 30), DAY)
    inp = ItemTrendsInput(restaurant_id=1, end_date=DAY, days=7, order_by="drop")
    result = queries.item_trends(fetch, inp, TABLES)

    assert (result.current_start, result.current_end) == (date(2026, 10, 21), DAY)
    assert (result.previous_start, result.previous_end) == (date(2026, 10, 14), date(2026, 10, 20))
    assert [i.menu_item_id for i in result.items] == [1, 3, 2]
    assert result.items[0].revenue_change == -200.0
    assert result.items[0].revenue_change_pct == -66.7
    assert result.items[1].revenue_change_pct is None  # nothing sold before: no %
    assert (result.total_revenue_current, result.total_revenue_previous) == (650.0, 500.0)
    sql, params = fetch.calls[0]
    assert params["current_start"] == date(2026, 10, 21)
    assert params["previous_start"] == date(2026, 10, 14)

    rise = queries.item_trends(fetch, inp.model_copy(update={"order_by": "rise"}), TABLES)
    assert [i.menu_item_id for i in rise.items] == [2, 3, 1]
    top = queries.item_trends(fetch, inp.model_copy(update={"limit": 1}), TABLES)
    assert len(top.items) == 1 and top.total_revenue_current == 650.0  # totals: every item


def test_item_trends_flags_a_partial_previous_window() -> None:
    fetch = FakeWarehouse([_item(1, 10.0, 5.0)], date(2026, 10, 18), DAY)
    inp = ItemTrendsInput(restaurant_id=1, end_date=DAY, days=7)
    note = queries.item_trends(fetch, inp, TABLES).note
    assert note is not None and "partial" in note


# Server and runtime wiring ------------------------------------------------------------


def _lake(fetch: FakeWarehouse) -> Lakehouse:
    @contextmanager
    def open_fetch():  # noqa: ANN202
        yield fetch

    return Lakehouse(open=open_fetch, tables=TABLES)


LAKEHOUSE_TOOLS = ("sales_vs_baseline", "item_trends")


def test_tools_are_absent_without_configuration() -> None:
    server = build_server()
    assert all(server._tool_manager.get_tool(t) is None for t in LAKEHOUSE_TOOLS)
    assert specialists.SALES.available_tools() == specialists.SALES.tools


def test_tools_are_registered_read_only_with_a_lakehouse() -> None:
    server = build_server(lakehouse=_lake(FakeWarehouse([], None, None)))
    for name in LAKEHOUSE_TOOLS:
        tool = server._tool_manager.get_tool(name)
        assert tool is not None
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert "restaurant_id" in tool.parameters["required"]


def test_partial_configuration_keeps_the_lakehouse_off() -> None:
    assert from_settings(Settings(databricks_server_hostname="h", databricks_http_path="p")) is None
    full = Settings(databricks_server_hostname="h", databricks_http_path="p", databricks_token="t")
    lake = from_settings(full)
    assert lake is not None and lake.tables == TABLES


def test_catalog_names_must_be_plain_identifiers() -> None:
    with pytest.raises(ValidationError):
        Settings(lakehouse_schema="brigada`; DROP TABLE x; --")


def test_sales_specialist_and_child_get_the_lakehouse_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = Settings(
        databricks_server_hostname="dbc-1.cloud.databricks.com",
        databricks_http_path="/sql/1.0/warehouses/abc",
        databricks_token="dapi-secret",
    )
    monkeypatch.setattr(specialists, "get_settings", lambda: configured)
    monkeypatch.setattr("brigada.runtime.mcp_tools.get_settings", lambda: configured)
    assert specialists.SALES.available_tools()[-2:] == LAKEHOUSE_TOOLS
    assert specialists.MENU.available_tools() == specialists.MENU.tools
    env = stdio_server().env or {}
    assert env["DATABRICKS_TOKEN"] == "dapi-secret"
    assert env["LAKEHOUSE_SCHEMA"] == "brigada"


def test_child_gets_no_databricks_variables_when_off() -> None:
    env = stdio_server().env or {}
    assert not any(k.startswith("DATABRICKS_") for k in env)


async def test_driver_errors_never_reach_the_model() -> None:
    @contextmanager
    def broken():  # noqa: ANN202
        raise RuntimeError("connection to dbc-secret.cloud.databricks.com refused")
        yield

    server = build_server(lakehouse=Lakehouse(open=broken, tables=TABLES))
    tool = server._tool_manager.get_tool("sales_vs_baseline")
    assert tool is not None
    with pytest.raises(ToolError) as info:
        await tool.fn(restaurant_id=1, start_date=DAY, end_date=DAY)
    assert "dbc-secret" not in str(info.value)
    assert "sales_by_period" in str(info.value)


async def test_tool_returns_the_query_result() -> None:
    fetch = FakeWarehouse([_channel("app", 0.0, 1200.0)], DAY, DAY)
    server = build_server(lakehouse=_lake(fetch))
    tool = server._tool_manager.get_tool("sales_vs_baseline")
    assert tool is not None
    result = await tool.fn(restaurant_id=1, start_date=DAY, end_date=DAY)
    assert result.days[0].channels[0].revenue_change_pct == -100.0
