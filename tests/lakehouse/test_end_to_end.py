"""Raw files -> medallion -> gold tables -> the exact SQL the MCP tools send, run by Spark SQL
(the dialect of a Databricks SQL warehouse). Catches what the fake warehouse cannot: column
names, parameter binding, aggregate functions."""

from datetime import date, timedelta
from typing import Any

import pytest

from brigada.lakehouse import queries
from brigada.lakehouse.models import (
    DeliveryPerformanceInput,
    ItemTrendsInput,
    SalesVsBaselineInput,
)
from brigada.lakehouse.queries import GoldTables
from scripts.seed_data import LATE_DELIVERY_RESTAURANT_ID, OUTAGE_RESTAURANT_ID, last_tuesday

from .conftest import END

pytestmark = pytest.mark.spark

TABLES = GoldTables("spark_catalog", "brigada_e2e")
GOLD = (
    "gold_restaurants",
    "gold_daily_channel_sales",
    "gold_item_daily_sales",
    "gold_delivery_hourly",
)
DC_STORE_3 = 1_000_003


@pytest.fixture(scope="module")
def fetch(spark: Any, tables: dict[str, Any]) -> queries.Fetch:
    spark.sql("CREATE DATABASE IF NOT EXISTS brigada_e2e")
    for name in GOLD:
        tables[name].write.mode("overwrite").saveAsTable(f"brigada_e2e.{name}")

    def run(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [r.asDict() for r in spark.sql(sql, args=params).collect()]

    return run


def test_sales_vs_baseline_finds_the_outage(fetch: queries.Fetch) -> None:
    outage = last_tuesday(END)
    inp = SalesVsBaselineInput(
        restaurant_id=OUTAGE_RESTAURANT_ID, start_date=outage - timedelta(days=6), end_date=END
    )
    result = queries.sales_vs_baseline(fetch, inp, TABLES)
    assert result.data_until == END and result.note is None and result.data_source == "brigada"
    assert len(result.days) == 7
    day = next(d for d in result.days if d.day == outage)
    assert [c.channel for c in day.channels] == ["app", "telefone", "balcao"]
    assert day.channels[0].orders == 0 and day.channels[0].revenue_change_pct == -100.0
    assert day.revenue_change_pct is not None and day.revenue_change_pct < -40
    others = [d.revenue_change_pct for d in result.days if d.day != outage]
    assert all(p is not None and p > -40 for p in others)


def test_item_trends_runs_on_spark_sql(fetch: queries.Fetch) -> None:
    inp = ItemTrendsInput(restaurant_id=OUTAGE_RESTAURANT_ID, end_date=END, days=7, limit=50)
    result = queries.item_trends(fetch, inp, TABLES)
    assert result.items and result.data_until == END and result.note is None
    changes = [i.revenue_change for i in result.items]
    assert changes == sorted(changes)  # "drop": biggest fall first


def test_delivery_performance_finds_saturday_night(fetch: queries.Fetch) -> None:
    inp = DeliveryPerformanceInput(
        restaurant_id=LATE_DELIVERY_RESTAURANT_ID,
        start_date=END - timedelta(days=89),
        end_date=END,
        group_by="weekday_hour",
    )
    result = queries.delivery_performance(fetch, inp, TABLES)
    by_key = {g.key: g for g in result.groups}
    late_night = [by_key[f"saturday {h}"] for h in range(18, 24) if f"saturday {h}" in by_key]
    rest = [
        g for k, g in by_key.items() if not (k.startswith("saturday ") and int(k.split()[1]) >= 18)
    ]
    assert late_night and rest
    for g in late_night:
        assert g.mean_promised_minutes and g.mean_delivery_minutes / g.mean_promised_minutes > 1.3
    assert result.overall is not None and result.overall.mean_distance_km is None


def test_real_dataset_restaurant_through_the_same_tools(fetch: queries.Fetch) -> None:
    jan = SalesVsBaselineInput(
        restaurant_id=DC_STORE_3, start_date=date(2021, 1, 4), end_date=date(2021, 1, 5)
    )
    sales = queries.sales_vs_baseline(fetch, jan, TABLES)
    assert sales.data_source == "delivery_center"
    tue = sales.days[-1]
    assert [c.channel for c in tue.channels] == ["marketplace", "canal_proprio"]
    assert tue.canceled_orders == 1 and tue.revenue == 80.0

    items = queries.item_trends(
        fetch, ItemTrendsInput(restaurant_id=DC_STORE_3, end_date=date(2021, 1, 5)), TABLES
    )
    assert items.items == [] and "no order lines" in (items.note or "")

    delivery = queries.delivery_performance(
        fetch,
        DeliveryPerformanceInput(
            restaurant_id=DC_STORE_3, start_date=date(2021, 1, 1), end_date=date(2021, 1, 5)
        ),
        TABLES,
    )
    assert delivery.overall is not None
    assert (
        delivery.overall.mean_delivery_minutes == 42.5 and delivery.overall.mean_distance_km == 2.0
    )
    assert delivery.overall.late_share is None  # no promises in this dataset


def test_stale_end_date_is_flagged(fetch: queries.Fetch) -> None:
    later = END + timedelta(days=2)
    inp = SalesVsBaselineInput(restaurant_id=1, start_date=END, end_date=later)
    result = queries.sales_vs_baseline(fetch, inp, TABLES)
    assert [d.day for d in result.days] == [END]
    assert result.note is not None and END.isoformat() in result.note
