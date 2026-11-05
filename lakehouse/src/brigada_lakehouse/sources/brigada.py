"""Brigada's own synthetic data (scripts/seed_data.py), exported by scripts/export_lakehouse.py.

Already clean by construction, so the work here is typing and the time zone: ``created_at_utc``
is a UTC wall clock without offset. Restaurant ids are kept as they are (1, 2, 3): they are the
same ids the Postgres tools use.
"""

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F

from brigada_lakehouse.common import (
    MENU_ITEM_COLUMNS,
    ORDER_COLUMNS,
    ORDER_ITEM_COLUMNS,
    RESTAURANT_COLUMNS,
    conform,
    latest_per_key,
    try_cast,
    utc_ntz_to_local,
    with_local_parts,
)

SOURCE = "brigada"
COUNTRY = "BR"
TIMEZONE = "America/Sao_Paulo"
CURRENCY = "BRL"
CHANNELS = ("app", "telefone", "balcao")
TABLES = ("restaurants", "menu_items", "orders", "order_items", "deliveries")


# Synthetic ids stay below the real datasets' bands (Delivery Center starts at 1_000_001).
MAX_RESTAURANT_ID = 999_999


def _restaurant_id(column: str) -> Column:
    raw = try_cast(column, "bigint")
    return F.when(raw.between(1, MAX_RESTAURANT_ID), raw)


def restaurants(bronze: DataFrame) -> DataFrame:
    df = bronze.select(
        _restaurant_id("id").alias("restaurant_id"),
        F.lit(SOURCE).alias("source"),
        F.trim("id").alias("source_id"),
        F.trim("name").alias("name"),
        F.trim("cuisine").alias("segment"),
        F.trim("city").alias("city"),
        F.lit(None).cast("string").alias("state"),
        F.lit(COUNTRY).alias("country"),
        F.lit(CURRENCY).alias("currency"),
        F.lit(TIMEZONE).alias("timezone"),
        *_meta(bronze),
    ).where("restaurant_id IS NOT NULL")
    return conform(latest_per_key(df, ["restaurant_id"]), RESTAURANT_COLUMNS)


def menu_items(bronze: DataFrame) -> DataFrame:
    df = bronze.select(
        _restaurant_id("restaurant_id").alias("restaurant_id"),
        try_cast("id", "bigint").alias("menu_item_id"),
        F.trim("name").alias("name"),
        F.trim("category").alias("category"),
        F.lower(F.trim("active")).isin("true", "t", "1").alias("active"),
        *_meta(bronze),
    )
    return conform(latest_per_key(df, ["menu_item_id"]), MENU_ITEM_COLUMNS)


def orders(bronze: DataFrame, deliveries: DataFrame) -> DataFrame:
    """Every synthetic order is completed; delivery minutes come from the deliveries table."""
    utc = try_cast("created_at_utc", "timestamp_ntz")
    df = bronze.select(
        F.trim("id").alias("order_id"),
        _restaurant_id("restaurant_id").alias("restaurant_id"),
        utc_ntz_to_local(utc, TIMEZONE).alias("created_at_local"),
        F.lower(F.trim("channel")).alias("channel"),
        F.lit("completed").alias("status"),
        try_cast("total", "decimal(12,2)").alias("total"),
        *_meta(bronze),
    )
    df = latest_per_key(df, ["order_id"])
    times = latest_per_key(
        deliveries.select(
            F.trim("order_id").alias("order_id"),
            try_cast("actual_minutes", "double").alias("delivery_minutes"),
            try_cast("promised_minutes", "double").alias("promised_minutes"),
            *_meta(deliveries),
        ),
        ["order_id"],
    ).drop(*_meta_names())
    df = (
        with_local_parts(df.drop(*_meta_names()).join(times, "order_id", "left"))
        .withColumn("source", F.lit(SOURCE))
        .withColumn("distance_km", F.lit(None).cast("double"))
        .withColumn("rating", F.lit(None).cast("double"))  # reviews stay in Postgres
    )
    for phase in ("prep_minutes", "pickup_minutes", "transit_minutes", "rider_wait_minutes"):
        df = df.withColumn(phase, F.lit(None).cast("double"))  # only the total is generated
    return conform(df, ORDER_COLUMNS)


def order_items(bronze: DataFrame, orders_silver: DataFrame) -> DataFrame:
    df = bronze.select(
        F.trim("order_id").alias("order_id"),
        try_cast("menu_item_id", "bigint").alias("menu_item_id"),
        try_cast("qty", "int").alias("qty"),
        try_cast("unit_price", "decimal(10,2)").alias("unit_price"),
        *_meta(bronze),
    )
    df = latest_per_key(df, ["order_id", "menu_item_id"]).drop(*_meta_names())
    owners = orders_silver.select("order_id", "restaurant_id")
    df = df.join(owners, "order_id").withColumn("source", F.lit(SOURCE))
    return conform(df, ORDER_ITEM_COLUMNS)


def _meta(df: DataFrame) -> list:
    return [F.col(c) for c in _meta_names() if c in df.columns]


def _meta_names() -> list[str]:
    return ["_ingested_at", "_source_file"]
