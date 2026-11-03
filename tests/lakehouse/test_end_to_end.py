"""Seed -> export CSV -> medallion transforms -> gold tables -> the exact SQL the MCP tools
send, run by Spark SQL (the dialect of a Databricks SQL warehouse). Catches what the fake
warehouse cannot: column names, parameter binding, aggregate functions."""

from datetime import date, timedelta
from typing import Any

import pytest

from brigada.lakehouse import queries
from brigada.lakehouse.models import ItemTrendsInput, SalesVsBaselineInput
from brigada.lakehouse.queries import GoldTables
from scripts.seed_data import OUTAGE_RESTAURANT_ID, last_tuesday

from .conftest import END

pytestmark = pytest.mark.spark

TABLES = GoldTables("spark_catalog", "brigada_e2e")


@pytest.fixture(scope="module")
def fetch(spark: Any, silver: dict[str, Any]) -> queries.Fetch:
    from brigada_lakehouse import transforms as t

    spark.sql("CREATE DATABASE IF NOT EXISTS brigada_e2e")
    t.gold_daily_channel_sales(silver["orders"]).write.mode("overwrite").saveAsTable(
        "brigada_e2e.gold_daily_channel_sales"
    )
    t.gold_item_daily_sales(
        silver["orders"], silver["order_items"], silver["menu_items"]
    ).write.mode("overwrite").saveAsTable("brigada_e2e.gold_item_daily_sales")

    def run(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [r.asDict() for r in spark.sql(sql, args=params).collect()]

    return run


def test_sales_vs_baseline_finds_the_outage(fetch: queries.Fetch) -> None:
    outage = last_tuesday(END)
    inp = SalesVsBaselineInput(
        restaurant_id=OUTAGE_RESTAURANT_ID, start_date=outage - timedelta(days=6), end_date=END
    )
    result = queries.sales_vs_baseline(fetch, inp, TABLES)
    assert result.data_until == END and result.note is None
    assert len(result.days) == 7
    day = next(d for d in result.days if d.day == outage)
    app = next(c for c in day.channels if c.channel == "app")
    assert app.orders == 0 and app.revenue_change_pct == -100.0
    assert day.revenue_change_pct is not None and day.revenue_change_pct < -40
    # Every other day of that week is close to its own baseline.
    others = [d.revenue_change_pct for d in result.days if d.day != outage]
    assert all(p is not None and p > -40 for p in others)


def test_item_trends_runs_on_spark_sql(fetch: queries.Fetch) -> None:
    inp = ItemTrendsInput(restaurant_id=OUTAGE_RESTAURANT_ID, end_date=END, days=7, limit=50)
    result = queries.item_trends(fetch, inp, TABLES)
    assert result.items and result.data_until == END
    assert result.note is None
    assert result.total_revenue_current > 0 and result.total_revenue_previous > 0
    changes = [i.revenue_change for i in result.items]
    assert changes == sorted(changes)  # "drop": biggest fall first


def test_stale_end_date_is_flagged(fetch: queries.Fetch) -> None:
    later = END + timedelta(days=2)
    inp = SalesVsBaselineInput(restaurant_id=1, start_date=END, end_date=later)
    result = queries.sales_vs_baseline(fetch, inp, TABLES)
    assert [d.day for d in result.days] == [END]
    assert result.note is not None and END.isoformat() in result.note
    assert isinstance(result.days[0].day, date)
