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
from brigada.lakehouse.models import (
    DeliveryPerformanceInput,
    ItemTrendsInput,
    SalesVsBaselineInput,
)
from brigada.lakehouse.queries import GoldTables
from brigada.lakehouse.warehouse import Lakehouse, from_settings
from brigada.mcp_server.server import build_server
from brigada.runtime import specialists
from brigada.runtime.mcp_tools import stdio_server

TABLES = GoldTables("workspace", "brigada")
DAY = date(2026, 10, 27)


class FakeWarehouse:
    """Answers the coverage query (gold_restaurants) and the main query with canned rows;
    records every call."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        data_from: date | None,
        until: date | None,
        source: str = "brigada",
        currency: str = "BRL",
    ):
        self.rows = rows
        self.coverage = (
            [{"source": source, "currency": currency, "data_from": data_from, "data_until": until}]
            if until
            else []
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        return self.coverage if "`gold_restaurants`" in sql else self.rows


def _channel(
    channel: str,
    revenue: float,
    baseline: float | None,
    weeks: int = 4,
    holiday: str | None = None,
) -> dict:
    return {
        "day": DAY,
        "channel": channel,
        "holiday_name": holiday,
        "orders": int(revenue // 50),
        "revenue": revenue,
        "canceled_orders": 1 if channel == "app" else 0,
        "baseline_weeks": weeks,
        "baseline_orders": None if baseline is None else baseline / 50,
        "baseline_revenue": baseline,
    }


def test_sales_vs_baseline_totals_the_channels_and_binds_every_value() -> None:
    fetch = FakeWarehouse(
        [
            _channel("balcao", 300.0, 280.0),
            _channel("app", 0.0, 1200.0),
            _channel("telefone", 0.0, 0.0),  # gold is zero-filled: every channel has a row
        ],
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
    assert telefone.orders == 0 and telefone.baseline_revenue == 0.0
    assert day.revenue == 300.0 and day.baseline_revenue == 1480.0
    assert day.revenue_change_pct == -79.7
    assert result.note is None
    assert day.canceled_orders == 1 and result.data_source == "brigada"
    assert result.currency == "BRL"
    assert "currency" in fetch.calls[1][0]  # read from gold_restaurants, not assumed
    main_sql, params = fetch.calls[0]
    assert "`workspace`.`brigada`.`gold_daily_channel_sales`" in main_sql
    for sql, params in fetch.calls:
        assert "2026-10-27" not in sql and params["restaurant_id"] == 1  # bound, not inlined


def test_a_holiday_is_named_on_its_day_and_in_the_note() -> None:
    fetch = FakeWarehouse(
        [_channel("app", 100.0, 1000.0, holiday="Sexta-feira da Paixão")], date(2026, 7, 30), DAY
    )
    inp = SalesVsBaselineInput(restaurant_id=1, start_date=DAY, end_date=DAY)
    result = queries.sales_vs_baseline(fetch, inp, TABLES)
    assert result.days[0].holiday == "Sexta-feira da Paixão"
    assert result.note is not None and "2026-10-27 (Sexta-feira da Paixão)" in result.note
    plain = queries.sales_vs_baseline(FakeWarehouse([_channel("app", 1, 1)], DAY, DAY), inp, TABLES)
    assert plain.days[0].holiday is None and plain.note is None


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
        "priced_rows": 14,
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
    assert result.items[0].qty_change == -20
    sql, params = fetch.calls[0]
    assert params["current_start"] == date(2026, 10, 21)
    assert params["previous_start"] == date(2026, 10, 14)

    rise = queries.item_trends(fetch, inp.model_copy(update={"order_by": "rise"}), TABLES)
    assert [i.menu_item_id for i in rise.items] == [2, 3, 1]
    top = queries.item_trends(fetch, inp.model_copy(update={"limit": 1}), TABLES)
    assert len(top.items) == 1 and top.total_revenue_current == 650.0  # totals: every item


def test_item_trends_without_prices_ranks_by_qty_and_never_says_zero() -> None:
    # Gold leaves revenue NULL for a source without item prices; the SQL's CASE ... ELSE 0
    # sums that to 0, so only priced_rows = 0 tells "unknown" from "sold nothing".
    rows = [
        {**_item(1, 0.0, 0.0), "qty_current": 3, "qty_previous": 9, "priced_rows": 0},
        {**_item(2, 0.0, 0.0), "qty_current": 8, "qty_previous": 2, "priced_rows": 0},
        {**_item(3, 0.0, 0.0), "category": None, "active": None, "priced_rows": 0},
    ]
    fetch = FakeWarehouse(
        rows, date(2024, 9, 1), date(2025, 1, 31), source="zomato", currency="INR"
    )
    inp = ItemTrendsInput(restaurant_id=120_320_607, end_date=date(2025, 1, 31), days=7)
    result = queries.item_trends(fetch, inp, TABLES)
    assert [i.menu_item_id for i in result.items] == [1, 3, 2]  # by qty change
    first = result.items[0]
    assert first.qty_change == -6
    assert first.revenue_current is None and first.revenue_change is None
    assert first.revenue_change_pct is None
    assert result.total_revenue_current is None and result.total_revenue_previous is None
    assert result.items[1].category is None and result.items[1].active is None
    assert result.currency == "INR"
    assert result.note is not None and "no item prices" in result.note
    top = queries.item_trends(fetch, inp.model_copy(update={"order_by": "revenue"}), TABLES)
    assert top.items[0].menu_item_id == 2  # most sold by qty


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


LAKEHOUSE_TOOLS = ("sales_vs_baseline", "item_trends", "delivery_performance")


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
    assert specialists.SALES.available_tools()[-2:] == LAKEHOUSE_TOOLS[:2]
    assert specialists.OPERATIONS.available_tools()[-1] == "delivery_performance"
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


def test_item_trends_says_when_the_dataset_has_no_order_lines() -> None:
    fetch = FakeWarehouse([], date(2021, 1, 1), date(2021, 4, 30), source="delivery_center")
    inp = ItemTrendsInput(restaurant_id=1_000_053, end_date=date(2021, 4, 30))
    result = queries.item_trends(fetch, inp, TABLES)
    assert result.items == [] and result.data_source == "delivery_center"
    assert result.note is not None and "no order lines" in result.note


def _hourly(
    k: str,
    n: int,
    minutes: float,
    promised: float,
    p_n: int,
    late: int,
    km: float,
    km_n: int,
    timed: int | None = None,
    phases: dict[str, tuple[float, int]] | None = None,
) -> dict:
    """phases: name -> (minutes sum, count); absent phases have none."""
    phase_cols = {}
    for name in ("prep", "pickup", "transit", "rider_wait"):
        total, count = (phases or {}).get(name, (0.0, 0))
        phase_cols[f"{name}_minutes_sum"] = total
        phase_cols[f"{name}_count"] = count
    return {
        **phase_cols,
        "k": k,
        "delivered_orders": n,
        "timed_orders": n if timed is None else timed,
        "delivery_minutes_sum": minutes,
        "promised_minutes_sum": promised,
        "promised_count": p_n,
        "late_orders": late,
        "distance_km_sum": km,
        "distance_count": km_n,
    }


def test_delivery_performance_averages_sums_and_sorts_by_calendar() -> None:
    rows = [
        _hourly("6 20", 10, 500.0, 400.0, 10, 8, 0.0, 0),  # saturday 20h
        _hourly("1 12", 4, 120.0, 160.0, 4, 0, 0.0, 0),  # monday 12h
        _hourly("6 9", 2, 60.0, 60.0, 2, 1, 0.0, 0),  # saturday 9h: before 20h
    ]
    fetch = FakeWarehouse(rows, date(2026, 7, 30), DAY)
    inp = DeliveryPerformanceInput(
        restaurant_id=3, start_date=date(2026, 10, 1), end_date=DAY, group_by="weekday_hour"
    )
    result = queries.delivery_performance(fetch, inp, TABLES)
    assert [g.key for g in result.groups] == ["monday 12", "saturday 9", "saturday 20"]
    sat = result.groups[-1]
    assert sat.mean_delivery_minutes == 50.0 and sat.mean_promised_minutes == 40.0
    assert sat.late_share == 0.8 and sat.mean_distance_km is None
    assert result.overall is not None and result.overall.delivered_orders == 16
    assert result.overall.mean_delivery_minutes == 42.5
    assert result.overall.late_share == 0.562
    sql, _ = fetch.calls[0]
    assert "concat(CAST(iso_weekday AS STRING)" in sql and "GROUP BY k" in sql


def test_delivery_performance_without_promises_or_deliveries() -> None:
    rows = [_hourly("all", 3, 120.0, 0.0, 0, 0, 6.0, 3)]
    fetch = FakeWarehouse(rows, date(2021, 1, 1), date(2021, 4, 30), source="delivery_center")
    inp = DeliveryPerformanceInput(
        restaurant_id=1_000_053, start_date=date(2021, 4, 1), end_date=date(2021, 4, 30)
    )
    result = queries.delivery_performance(fetch, inp, TABLES)
    assert result.groups == []
    assert result.overall is not None
    assert result.overall.mean_promised_minutes is None and result.overall.late_share is None
    assert result.overall.mean_distance_km == 2.0

    assert result.overall.timed_orders == 3 and result.currency == "BRL"

    empty = queries.delivery_performance(FakeWarehouse([], None, None), inp, TABLES)
    assert empty.overall is None and "no data" in (empty.note or "")


def test_delivery_performance_without_delivery_times_says_unknown() -> None:
    rows = [_hourly("20", 4, 0.0, 0.0, 0, 0, 10.0, 4, timed=0)]
    fetch = FakeWarehouse(
        rows, date(2024, 9, 1), date(2025, 1, 31), source="zomato", currency="INR"
    )
    inp = DeliveryPerformanceInput(
        restaurant_id=120_320_607,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
        group_by="hour",
    )
    result = queries.delivery_performance(fetch, inp, TABLES)
    assert result.overall is not None and result.overall.delivered_orders == 4
    assert result.overall.timed_orders == 0 and result.overall.mean_delivery_minutes is None
    assert result.overall.mean_distance_km == 2.5
    assert result.groups[0].mean_delivery_minutes is None
    assert result.note is not None and "no delivery time" in result.note


def test_delivery_performance_splits_the_time_into_phases() -> None:
    # Delivery Center shape: kitchen, food waiting for the courier, transit (they add up).
    phases = {"prep": (500.0, 20), "pickup": (100.0, 20), "transit": (300.0, 20)}
    rows = [
        _hourly("20", 20, 900.0, 0.0, 0, 0, 30.0, 20, phases=phases),
        _hourly("12", 10, 300.0, 0.0, 0, 0, 15.0, 10),  # no phase recorded at noon
    ]
    fetch = FakeWarehouse(rows, date(2021, 1, 1), date(2021, 4, 30), source="delivery_center")
    inp = DeliveryPerformanceInput(
        restaurant_id=1_000_676,
        start_date=date(2021, 4, 1),
        end_date=date(2021, 4, 30),
        group_by="hour",
    )
    result = queries.delivery_performance(fetch, inp, TABLES)
    noon, night = result.groups
    assert night.mean_prep_minutes == 25.0 and night.mean_pickup_minutes == 5.0
    assert night.mean_transit_minutes == 15.0 and night.prep_orders == 20
    assert night.mean_rider_wait_minutes is None  # this source does not record it
    assert noon.mean_prep_minutes is None and noon.prep_orders == 0  # unknown, not 0
    assert result.overall is not None and result.overall.mean_prep_minutes == 25.0


def test_zomato_has_kitchen_time_without_delivery_time() -> None:
    phases = {"prep": (180.0, 10), "rider_wait": (80.0, 10)}
    rows = [_hourly("all", 10, 0.0, 0.0, 0, 0, 40.0, 10, timed=0, phases=phases)]
    fetch = FakeWarehouse(
        rows, date(2024, 9, 1), date(2025, 1, 31), source="zomato", currency="INR"
    )
    inp = DeliveryPerformanceInput(
        restaurant_id=120_635_699, start_date=date(2025, 1, 1), end_date=date(2025, 1, 31)
    )
    result = queries.delivery_performance(fetch, inp, TABLES)
    assert result.overall is not None and result.overall.mean_delivery_minutes is None
    assert result.overall.mean_prep_minutes == 18.0
    assert result.overall.mean_rider_wait_minutes == 8.0
    assert result.note is not None and "kitchen time" in result.note


def test_a_restaurant_id_in_two_sources_fails_closed() -> None:
    fetch = FakeWarehouse([_channel("app", 100.0, 100.0)], DAY, DAY)
    fetch.coverage = fetch.coverage * 2  # the same id from two sources
    inp = SalesVsBaselineInput(restaurant_id=1, start_date=DAY, end_date=DAY)
    with pytest.raises(RuntimeError, match="appears 2 times"):
        queries.sales_vs_baseline(fetch, inp, TABLES)


def test_no_order_lines_means_no_revenue_total_not_zero() -> None:
    fetch = FakeWarehouse([], date(2021, 1, 1), date(2021, 4, 30), source="delivery_center")
    inp = ItemTrendsInput(restaurant_id=1_000_053, end_date=date(2021, 4, 30))
    result = queries.item_trends(fetch, inp, TABLES)
    assert result.total_revenue_current is None and result.total_revenue_previous is None


def test_days_before_the_data_starts_are_missing_not_zero() -> None:
    fetch = FakeWarehouse([], date(2021, 1, 2), date(2021, 4, 30), source="delivery_center")
    before = SalesVsBaselineInput(
        restaurant_id=1_000_053, start_date=date(2020, 12, 1), end_date=date(2020, 12, 3)
    )
    note = queries.sales_vs_baseline(fetch, before, TABLES).note
    assert note is not None and "only from 2021-01-02" in note and "before it" in note
    straddle = before.model_copy(
        update={"end_date": date(2021, 1, 1), "start_date": date(2020, 12, 25)}
    )
    assert "missing" in (queries.sales_vs_baseline(fetch, straddle, TABLES).note or "")


def test_a_holiday_note_keeps_the_missing_days_note() -> None:
    fetch = FakeWarehouse(
        [_channel("app", 100.0, 1000.0, holiday="Sexta-feira da Paixão")], date(2026, 7, 30), DAY
    )
    inp = SalesVsBaselineInput(restaurant_id=1, start_date=DAY, end_date=date(2026, 10, 29))
    note = queries.sales_vs_baseline(fetch, inp, TABLES).note or ""
    assert "missing, not zero" in note and "Sexta-feira da Paixão" in note


def test_mean_delivery_divides_by_the_timed_orders_only() -> None:
    # 10 delivered, 6 with a time (the other 4 only have a distance): 300 / 6, not 300 / 10.
    rows = [_hourly("20", 10, 300.0, 0.0, 0, 0, 20.0, 10, timed=6)]
    fetch = FakeWarehouse(rows, date(2021, 1, 1), date(2021, 4, 30), source="delivery_center")
    inp = DeliveryPerformanceInput(
        restaurant_id=1_000_053, start_date=date(2021, 4, 1), end_date=date(2021, 4, 30)
    )
    overall = queries.delivery_performance(fetch, inp, TABLES).overall
    assert overall is not None and overall.mean_delivery_minutes == 50.0
    assert overall.delivered_orders == 10 and overall.timed_orders == 6


def test_the_result_names_the_table_not_the_catalog() -> None:
    fetch = FakeWarehouse([], DAY, DAY)
    inp = SalesVsBaselineInput(restaurant_id=1, start_date=DAY, end_date=DAY)
    source = queries.sales_vs_baseline(fetch, inp, TABLES).source
    assert source == "lakehouse gold_daily_channel_sales" and "workspace" not in source


def test_item_trends_rejects_an_ancient_end_date() -> None:
    with pytest.raises(ValidationError, match="on or after 2000-01-01"):
        ItemTrendsInput(restaurant_id=1, end_date=date(1, 1, 3))
