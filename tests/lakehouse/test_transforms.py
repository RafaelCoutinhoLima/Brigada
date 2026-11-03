"""The pipeline transforms on a local SparkSession, fed by the real export.

The seed data goes through ``scripts/export_lakehouse.py`` to CSV, is read with the same reader
as the pipeline (``read_raw``) and runs bronze -> silver -> gold. The planted outage (restaurant
1, app down on the last Tuesday) must come out of gold as a -100% app day against its baseline.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

pyspark = pytest.importorskip("pyspark")

from brigada_lakehouse import transforms as t  # noqa: E402
from pyspark.sql import DataFrame, SparkSession  # noqa: E402

from scripts.export_lakehouse import to_csv  # noqa: E402
from scripts.seed_data import OUTAGE_RESTAURANT_ID, generate, last_tuesday, planted_facts  # noqa: E402

pytestmark = pytest.mark.spark

from .conftest import END  # noqa: E402


def test_export_writes_utc_wall_clock_and_plain_decimals() -> None:
    ts = datetime(2026, 10, 28, 1, 30, tzinfo=UTC)
    text = to_csv(("id", "created_at", "total", "active"), [(1, ts, Decimal("12.50"), True)])
    assert text.splitlines() == [
        "id,created_at_utc,total,active",
        "1,2026-10-28 01:30:00,12.50,true",
    ]


def test_silver_keeps_every_order_and_its_local_day(silver: dict[str, DataFrame]) -> None:
    dataset = generate(END)
    assert silver["orders"].count() == len(dataset.orders)
    assert silver["order_items"].count() == len(dataset.order_items)
    assert silver["menu_items"].count() == len(dataset.menu_items)
    # Local day computed by Python (zoneinfo) for a sample of orders equals Spark's.
    expected = {o.id: o.created_at.astimezone(t_zone()).date() for o in dataset.orders[:300]}
    got = {
        r["id"]: r["local_day"]
        for r in silver["orders"].where(silver["orders"].id.isin(list(expected))).collect()
    }
    assert got == expected


def t_zone():  # noqa: ANN201
    from zoneinfo import ZoneInfo

    return ZoneInfo(t.LOCAL_TZ)


def test_order_after_21h_local_belongs_to_that_local_day(spark: SparkSession) -> None:
    # 2026-10-28 01:30 UTC is 2026-10-27 22:30 in Sao Paulo (UTC-3).
    bronze = spark.createDataFrame(
        [("1", "1", "2026-10-28 01:30:00", "40.00", "app")],
        ["id", "restaurant_id", "created_at_utc", "total", "channel"],
    )
    row = t.silver_orders(bronze).first()
    assert row["local_day"] == date(2026, 10, 27)
    assert row["local_hour"] == 22
    assert row["iso_weekday"] == 2  # Tuesday


def test_expectations_drop_bad_rows(spark: SparkSession) -> None:
    bronze = spark.createDataFrame(
        [
            ("1", "1", "2026-10-27 12:00:00", "40.00", "app"),
            ("2", "1", "2026-10-27 12:00:00", "40.00", "ifood"),  # unknown channel
            ("3", "1", "not a date", "40.00", "app"),
            ("4", "1", "2026-10-27 12:00:00", "-1.00", "balcao"),
        ],
        ["id", "restaurant_id", "created_at_utc", "total", "channel"],
    )
    kept = t.apply_expectations(t.silver_orders(bronze), t.ORDER_EXPECTATIONS)
    assert [r["id"] for r in kept.collect()] == [1]


def test_gold_channel_sales_is_zero_filled_and_baseline_uses_previous_same_weekdays(
    spark: SparkSession,
) -> None:
    # Restaurant 1: one app order every Tuesday for 5 weeks, R$ 10, 20, 30, 40 then 0 orders
    # on the 5th Tuesday (only a telefone order that day).
    tuesdays = [date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 20)]
    rows = [
        (str(i + 1), "1", f"{d.isoformat()} 15:00:00", f"{(i + 1) * 10}.00", "app")
        for i, d in enumerate(tuesdays)
    ]
    rows.append(("9", "1", "2026-10-27 15:00:00", "5.00", "telefone"))
    orders = t.silver_orders(
        spark.createDataFrame(rows, ["id", "restaurant_id", "created_at_utc", "total", "channel"])
    )
    gold = t.gold_daily_channel_sales(orders)

    days = (date(2026, 10, 27) - date(2026, 9, 29)).days + 1
    assert gold.count() == days * len(t.CHANNELS)  # every day x channel, even empty ones

    last = {r["channel"]: r for r in gold.where("day = '2026-10-27'").collect()}
    assert last["app"]["orders"] == 0
    assert last["app"]["baseline_weeks"] == 4
    assert last["app"]["baseline_revenue"] == Decimal("25.00")  # mean of 10, 20, 30, 40
    assert last["app"]["revenue_change_pct"] == -100.0
    assert last["balcao"]["baseline_revenue"] == Decimal("0.00")
    assert last["balcao"]["revenue_change_pct"] is None  # no division by a zero baseline

    first = gold.where("day = '2026-09-29' AND channel = 'app'").first()
    assert first["baseline_weeks"] == 0
    assert first["baseline_revenue"] is None  # no history yet: unknown, not 0


def test_gold_reproduces_the_planted_outage(silver: dict[str, DataFrame]) -> None:
    gold = t.gold_daily_channel_sales(silver["orders"])
    outage = last_tuesday(END)
    rows = {
        r["channel"]: r
        for r in gold.where(
            (gold.restaurant_id == OUTAGE_RESTAURANT_ID) & (gold.day == outage)
        ).collect()
    }
    assert rows["app"]["orders"] == 0
    assert rows["app"]["revenue_change_pct"] == -100.0
    assert rows["app"]["baseline_weeks"] == 4
    # Only the app collapsed: the other channels kept selling and none fell like it.
    for channel in ("telefone", "balcao"):
        assert rows[channel]["orders"] > 0
        assert rows[channel]["revenue_change_pct"] > -50
    # Same day total as the generator's own ground truth.
    facts = planted_facts(generate(END))["outage"]
    assert facts["date"] == outage.isoformat()
    assert sum(r["orders"] for r in rows.values()) == facts["total_orders"]


def test_gold_item_sales_matches_the_order_lines(silver: dict[str, DataFrame]) -> None:
    gold = t.gold_item_daily_sales(silver["orders"], silver["order_items"], silver["menu_items"])
    dataset = generate(END)
    expected_revenue = sum(i.qty * i.unit_price for i in dataset.order_items)
    expected_qty = sum(i.qty for i in dataset.order_items)
    totals = gold.agg({"revenue": "sum", "qty": "sum"}).first()
    assert totals["sum(qty)"] == expected_qty
    assert Decimal(totals["sum(revenue)"]) == expected_revenue
    # Zero-filled: every item of each restaurant appears on every day of its calendar.
    per_restaurant_items = {}
    for m in dataset.menu_items:
        per_restaurant_items[m.restaurant_id] = per_restaurant_items.get(m.restaurant_id, 0) + 1
    calendar = t.gold_daily_channel_sales(silver["orders"]).where("channel = 'app'")
    days = {
        r["restaurant_id"]: r["n"]
        for r in calendar.groupBy("restaurant_id").count().withColumnRenamed("count", "n").collect()
    }
    assert gold.count() == sum(days[r] * n for r, n in per_restaurant_items.items())
