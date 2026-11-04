"""The medallion on a local SparkSession, fed exactly like the raw volume: the synthetic export
and a Delivery Center extract with the real files' defects."""

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("pyspark")

from brigada_lakehouse import common, gold  # noqa: E402
from brigada_lakehouse.build import SOURCES  # noqa: E402
from brigada_lakehouse.sources import brigada, delivery_center  # noqa: E402

from scripts.export_lakehouse import to_csv  # noqa: E402
from scripts.seed_data import OUTAGE_RESTAURANT_ID, generate, last_tuesday, planted_facts  # noqa: E402
from scripts.upload_source import SOURCES as UPLOAD_SOURCES  # noqa: E402

from .conftest import END  # noqa: E402

pytestmark = pytest.mark.spark

DC = delivery_center.RESTAURANT_ID_OFFSET


def _rows(df: Any, key: str) -> dict[Any, Any]:
    return {r[key]: r for r in df.collect()}


# Wiring -----------------------------------------------------------------------------------


def test_pipeline_defines_the_same_tables_as_build(tables: dict[str, Any]) -> None:
    text = (Path(__file__).parents[2] / "lakehouse/src/pipeline.py").read_text()
    defined = set(re.findall(r"def ((?:silver|gold)_\w+)\(\)", text))
    built = {n for n in tables if not n.startswith("bronze_")}
    assert defined == built


def test_upload_script_knows_the_tables_of_each_source() -> None:
    for source, tables in UPLOAD_SOURCES.items():
        assert tables == SOURCES[source].TABLES


# Synthetic source -------------------------------------------------------------------------


def test_export_writes_utc_wall_clock_and_plain_decimals() -> None:
    ts = datetime(2026, 10, 28, 1, 30, tzinfo=UTC)
    text = to_csv(("id", "created_at", "total", "active"), [(1, ts, Decimal("12.50"), True)])
    assert text.splitlines() == [
        "id,created_at_utc,total,active",
        "1,2026-10-28 01:30:00,12.50,true",
    ]


def test_brigada_silver_keeps_every_order_with_local_day_and_delivery(
    tables: dict[str, Any],
) -> None:
    dataset = generate(END)
    orders = tables["silver_brigada_orders"]
    assert orders.count() == len(dataset.orders)
    assert tables["silver_brigada_order_items"].count() == len(dataset.order_items)
    tz = ZoneInfo(brigada.TIMEZONE)
    sample = {str(o.id): o.created_at.astimezone(tz).date() for o in dataset.orders[:300]}
    got = {
        r["order_id"]: r["local_day"]
        for r in orders.where(orders.order_id.isin(list(sample))).collect()
    }
    assert got == sample
    deliveries = {str(d.order_id): d for d in dataset.deliveries}
    one = next(iter(deliveries))
    row = orders.where(orders.order_id == one).first()
    assert row["delivery_minutes"] == deliveries[one].actual_minutes
    assert row["promised_minutes"] == deliveries[one].promised_minutes
    assert row["status"] == "completed" and row["source"] == "brigada"


def test_order_after_21h_local_belongs_to_that_local_day(spark: Any) -> None:
    # 2026-10-28 01:30 UTC is 2026-10-27 22:30 in Sao Paulo (UTC-3).
    bronze = spark.createDataFrame(
        [("1", "1", "2026-10-28 01:30:00", "40.00", "app")],
        ["id", "restaurant_id", "created_at_utc", "total", "channel"],
    )
    deliveries = spark.createDataFrame(
        [], "order_id string, actual_minutes string, promised_minutes string"
    )
    row = brigada.orders(bronze, deliveries).first()
    assert row["local_day"] == date(2026, 10, 27)
    assert row["local_hour"] == 22 and row["iso_weekday"] == 2
    assert row["delivery_minutes"] is None


def test_raw_headers_become_delta_safe_names() -> None:
    assert common.column_name("Restaurant ID") == "restaurant_id"
    assert (
        common.column_name("Restaurant discount (Flat offs, Freebies & others)")
        == "restaurant_discount_flat_offs_freebies_others"
    )
    assert common.column_name("KPT duration (minutes)") == "kpt_duration_minutes"
    assert common.column_name("order_moment_created") == "order_moment_created"  # unchanged


def test_time_zone_is_the_sources_not_the_sessions(spark: Any) -> None:
    # The session zone is Asia/Tokyo (conftest): neither result may depend on it.
    df = spark.createDataFrame([("1/5/2021 12:30:00 AM",)], ["t"])
    fmt = "M/d/yyyy h:mm:ss a"
    row = df.select(
        common.text_to_wall_clock("t", fmt).alias("wall"),
        common.utc_text_to_local("t", fmt, "America/Sao_Paulo").alias("sp"),
        common.utc_text_to_local("t", fmt, "Asia/Kolkata").alias("india"),
    ).first()
    assert row["wall"] == datetime(2021, 1, 5, 0, 30)
    assert row["sp"] == datetime(2021, 1, 4, 21, 30)  # UTC-3
    assert row["india"] == datetime(2021, 1, 5, 6, 0)  # UTC+5:30


# Delivery Center ----------------------------------------------------------------------------


def test_dc_restaurants_are_the_food_stores_with_clean_names(tables: dict[str, Any]) -> None:
    rows = _rows(tables["silver_delivery_center_restaurants"], "restaurant_id")
    assert set(rows) == {DC + 3, DC + 4}  # store 5 is GOOD
    assert rows[DC + 3]["name"] == "PAPA SUCIS"  # doubled and trailing spaces
    assert rows[DC + 4]["name"] == "ÂRPIR PILMACI"  # Latin-1 read right
    assert rows[DC + 4]["city"] == "São Paulo" and rows[DC + 4]["state"] == "SP"


def test_dc_orders_are_cleaned(tables: dict[str, Any]) -> None:
    rows = _rows(tables["silver_delivery_center_orders"], "order_id")
    # 3 absurd amount, 4 zero, 6 GOOD store, 7 impossible date, 8 unknown channel.
    assert set(rows) == {"1", "2", "5", "9"}
    first = rows["1"]
    assert first["created_at_local"] == datetime(2021, 1, 4, 21, 30)  # UTC -> UTC-3
    assert first["local_day"] == date(2021, 1, 4) and first["iso_weekday"] == 1
    assert first["channel"] == "marketplace" and first["status"] == "completed"
    assert first["delivery_minutes"] == 42.5
    assert first["distance_km"] == 2.0  # first DELIVERED row, not the duplicate
    assert rows["2"]["channel"] == "canal_proprio" and rows["2"]["status"] == "canceled"
    assert rows["5"]["delivery_minutes"] is None  # open for days: unknown, order kept
    assert rows["5"]["distance_km"] is None  # 7,251 km
    assert rows["9"]["distance_km"] is None  # only a cancelled delivery


def test_gold_channels_follow_the_source(tables: dict[str, Any]) -> None:
    g = tables["gold_daily_channel_sales"]
    dc = g.where(g.restaurant_id == DC + 3)
    assert {r["channel"] for r in dc.collect()} == set(delivery_center.CHANNELS)
    tue = _rows(dc.where("day = '2021-01-05'"), "channel")
    assert tue["canal_proprio"]["orders"] == 0 and tue["canal_proprio"]["canceled_orders"] == 1
    assert tue["marketplace"]["revenue"] == Decimal("80.00")
    syn = g.where(g.restaurant_id == 1)
    assert {r["channel"] for r in syn.select("channel").distinct().collect()} == set(
        brigada.CHANNELS
    )


def test_gold_restaurants_has_coverage(tables: dict[str, Any]) -> None:
    rows = _rows(tables["gold_restaurants"], "restaurant_id")
    assert (rows[1]["currency"], rows[1]["timezone"]) == ("BRL", "America/Sao_Paulo")
    assert (rows[DC + 3]["currency"], rows[DC + 3]["timezone"]) == ("BRL", "America/Sao_Paulo")
    assert rows[DC + 3]["data_from"] == date(2021, 1, 4)
    assert rows[DC + 3]["data_until"] == date(2021, 1, 5)
    assert rows[DC + 3]["completed_orders"] == 2
    assert rows[1]["source"] == "brigada" and rows[1]["data_until"] == END


def test_gold_delivery_hourly_sums_and_counts(tables: dict[str, Any]) -> None:
    g = tables["gold_delivery_hourly"]
    dc = g.where(g.restaurant_id == DC + 3).collect()
    assert len(dc) == 1  # order 5 has no delivery time; 2 is canceled
    (row,) = dc
    assert (row["day"], row["hour"]) == (date(2021, 1, 4), 21)
    assert row["delivered_orders"] == 1 and row["delivery_minutes_sum"] == 42.5
    assert row["distance_count"] == 1 and row["promised_count"] == 0 and row["late_orders"] == 0
    syn = g.where(g.restaurant_id == 1).agg({"promised_count": "sum", "late_orders": "sum"}).first()
    assert syn[0] > 0 and syn[1] > 0  # the synthetic source has promises and late orders


def test_items_only_from_sources_that_have_them(tables: dict[str, Any]) -> None:
    items = tables["gold_item_daily_sales"]
    assert items.where(items.restaurant_id >= DC).count() == 0


# Gold logic -------------------------------------------------------------------------------


def test_baseline_uses_previous_same_weekdays_and_never_invents_zero(spark: Any) -> None:
    tuesdays = [date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 20)]
    rows = [
        (str(i + 1), "1", f"{d.isoformat()} 15:00:00", f"{(i + 1) * 10}.00", "app")
        for i, d in enumerate(tuesdays)
    ]
    rows.append(("9", "1", "2026-10-27 15:00:00", "5.00", "telefone"))
    bronze = spark.createDataFrame(
        rows, ["id", "restaurant_id", "created_at_utc", "total", "channel"]
    )
    deliveries = spark.createDataFrame(
        [], "order_id string, actual_minutes string, promised_minutes string"
    )
    g = gold.gold_daily_channel_sales(brigada.orders(bronze, deliveries))

    days = (date(2026, 10, 27) - date(2026, 9, 29)).days + 1
    assert g.count() == days * len(brigada.CHANNELS)  # every day x channel
    last = _rows(g.where("day = '2026-10-27'"), "channel")
    assert last["app"]["orders"] == 0 and last["app"]["baseline_weeks"] == 4
    assert last["app"]["baseline_revenue"] == Decimal("25.00")  # mean of 10, 20, 30, 40
    assert last["app"]["revenue_change_pct"] == -100.0
    assert last["balcao"]["revenue_change_pct"] is None  # no division by a zero baseline
    first = g.where("day = '2026-09-29' AND channel = 'app'").first()
    assert first["baseline_weeks"] == 0 and first["baseline_revenue"] is None


def test_gold_reproduces_the_planted_outage(tables: dict[str, Any]) -> None:
    g = tables["gold_daily_channel_sales"]
    outage = last_tuesday(END)
    rows = _rows(g.where((g.restaurant_id == OUTAGE_RESTAURANT_ID) & (g.day == outage)), "channel")
    assert rows["app"]["orders"] == 0 and rows["app"]["revenue_change_pct"] == -100.0
    for channel in ("telefone", "balcao"):
        assert rows[channel]["orders"] > 0 and rows[channel]["revenue_change_pct"] > -50
    facts = planted_facts(generate(END))["outage"]
    assert sum(r["orders"] for r in rows.values()) == facts["total_orders"]


def test_gold_item_sales_matches_the_order_lines(tables: dict[str, Any]) -> None:
    items = tables["gold_item_daily_sales"]
    dataset = generate(END)
    totals = items.agg({"revenue": "sum", "qty": "sum"}).first()
    assert totals["sum(qty)"] == sum(i.qty for i in dataset.order_items)
    assert Decimal(totals["sum(revenue)"]) == sum(i.qty * i.unit_price for i in dataset.order_items)


def test_expectations_drop_bad_rows(spark: Any) -> None:
    bronze = spark.createDataFrame(
        [
            ("1", "1", "2026-10-27 12:00:00", "40.00", "app"),
            ("3", "1", "not a date", "40.00", "app"),
            ("4", "1", "2026-10-27 12:00:00", "-1.00", "balcao"),
            ("5", "1", "2026-10-27 12:00:00", "abc", "balcao"),  # malformed: NULL, not a crash
        ],
        ["id", "restaurant_id", "created_at_utc", "total", "channel"],
    )
    deliveries = spark.createDataFrame(
        [], "order_id string, actual_minutes string, promised_minutes string"
    )
    kept = common.apply_expectations(brigada.orders(bronze, deliveries), common.ORDER_EXPECTATIONS)
    assert [r["order_id"] for r in kept.collect()] == ["1"]


# The real dataset (local only) ---------------------------------------------------------------


def test_real_delivery_center_download(
    spark: Any, real_delivery_center_dir: Path, tmp_path: Path
) -> None:
    from brigada_lakehouse.build import build

    from scripts.upload_source import main as upload_main

    assert (
        upload_main(["delivery_center", str(real_delivery_center_dir), "--out-dir", str(tmp_path)])
        == 0
    )
    t = build(spark, str(tmp_path), ["delivery_center"])
    raw = t["bronze_delivery_center_orders"].count()
    kept = t["silver_delivery_center_orders"].count()
    restaurants = t["silver_delivery_center_restaurants"].count()
    print(f"\nDelivery Center: {raw} raw orders, {kept} kept, {restaurants} FOOD restaurants")
    assert raw > 360_000 and 0.8 * raw < kept < raw and restaurants > 300
    hours = {
        r["local_hour"]: r["count"]
        for r in t["silver_delivery_center_orders"].groupBy("local_hour").count().collect()
    }
    # After UTC -> local, lunch (12h) and dinner (19-20h) are the peaks, not 15h and 22h.
    assert hours[12] > hours[15] and hours[19] > hours[22]
