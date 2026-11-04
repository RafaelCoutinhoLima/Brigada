"""Delivery Center: Food & Goods orders in Brazil (Kaggle ``nosbielcs/brazilian-delivery-center``).

~369k orders from January to April 2021 of a platform that connects stores to marketplaces,
anonymized by its authors. Only the FOOD stores become Brigada restaurants; GOOD (retail) is out
of scope. The dataset has no order lines and no reviews: item and review questions have no
answer for these restaurants, and the tools say so.

What the profiling of the raw files found, and what this module does about it:

- ``order_moment_created`` ("1/1/2021 12:01:36 AM") is **UTC**: the hourly volume peaks at 15h
  and 22-23h, which is lunch and dinner only in UTC-3. Converted to America/Sao_Paulo.
- ``stores.csv`` and ``hubs.csv`` are Latin-1 (``SÃO PAULO``); store names carry trailing and
  doubled spaces. Read with the right encoding, whitespace normalized.
- 40 channels collapse into the two that matter to an owner: marketplace vs own channel.
- ``order_amount`` has zeros and absurd values (max R$ 1.79 million; p99.9 is R$ 1,500):
  orders outside (0, ``MAX_ORDER_AMOUNT``] are dropped by an expectation and counted.
- ``order_metric_cycle_time`` (minutes, order to delivery) has orders left open for days
  (p99 is ~45 hours): kept as the order's delivery time only when in (0, ``MAX_DELIVERY_MINUTES``],
  else NULL. The order still counts for revenue.
- ``deliveries.csv`` has more than one row for ~20k orders and distances up to 7,251 km: the
  first DELIVERED row (lowest ``delivery_id``) is the order's, and only (0, ``MAX_DISTANCE_M``]
  is kept.
- ``order_status`` FINISHED / CANCELED map to completed / canceled.
"""

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

from brigada_lakehouse.common import (
    ORDER_COLUMNS,
    RESTAURANT_COLUMNS,
    clean_text,
    conform,
    latest_per_key,
    try_cast,
    utc_text_to_local,
    with_local_parts,
)

SOURCE = "delivery_center"
# Brigada restaurant id = offset + store_id, so the ids never collide with other sources.
RESTAURANT_ID_OFFSET = 1_000_000
CHANNELS = ("marketplace", "canal_proprio")
TABLES = ("orders", "stores", "hubs", "channels", "deliveries")
ENCODINGS = {"stores": "ISO-8859-1", "hubs": "ISO-8859-1"}
TIMESTAMP_FORMAT = "M/d/yyyy h:mm:ss a"

MAX_ORDER_AMOUNT = 5000
MAX_DELIVERY_MINUTES = 240
MAX_DISTANCE_M = 30_000

ORDER_EXPECTATIONS = {
    "plausible_amount": f"total > 0 AND total <= {MAX_ORDER_AMOUNT}",
}
_CHANNEL_TYPES = {"MARKETPLACE": "marketplace", "OWN CHANNEL": "canal_proprio"}
_STATUSES = {"FINISHED": "completed", "CANCELED": "canceled"}


def restaurants(stores: DataFrame, hubs: DataFrame) -> DataFrame:
    """FOOD stores, with the city and state of their hub."""
    hub = latest_per_key(
        hubs.select(
            try_cast("hub_id", "int").alias("hub_id"),
            F.initcap(clean_text("hub_city")).alias("city"),
            F.upper(clean_text("hub_state")).alias("state"),
            *_meta(hubs),
        ),
        ["hub_id"],
    ).drop(*_meta_names())
    store = latest_per_key(
        stores.select(
            try_cast("store_id", "bigint").alias("store_id"),
            try_cast("hub_id", "int").alias("hub_id"),
            clean_text("store_name").alias("name"),
            F.upper(F.trim("store_segment")).alias("store_segment"),
            *_meta(stores),
        ).where("store_id IS NOT NULL"),
        ["store_id"],
    ).drop(*_meta_names())
    df = (
        store.where("store_segment = 'FOOD'")
        .join(hub, "hub_id", "left")
        .select(
            (F.col("store_id") + RESTAURANT_ID_OFFSET).alias("restaurant_id"),
            F.lit(SOURCE).alias("source"),
            F.col("store_id").cast("string").alias("source_id"),
            F.coalesce("name", F.concat(F.lit("Loja "), F.col("store_id"))).alias("name"),
            F.lit("food").alias("segment"),
            "city",
            "state",
        )
    )
    return conform(df, RESTAURANT_COLUMNS)


def _delivery_per_order(deliveries: DataFrame) -> DataFrame:
    df = deliveries.select(
        try_cast("delivery_id", "bigint").alias("delivery_id"),
        F.trim("delivery_order_id").alias("order_id"),
        try_cast("delivery_distance_meters", "double").alias("meters"),
        F.upper(F.trim("delivery_status")).alias("delivery_status"),
    ).where("delivery_status = 'DELIVERED' AND order_id IS NOT NULL")
    first = Window.partitionBy("order_id").orderBy(F.col("delivery_id").asc_nulls_last())
    df = df.withColumn("_rn", F.row_number().over(first)).where("_rn = 1")
    km = F.when((F.col("meters") > 0) & (F.col("meters") <= MAX_DISTANCE_M), F.col("meters") / 1000)
    return df.select("order_id", km.alias("distance_km"))


def orders(
    bronze: DataFrame, channels: DataFrame, deliveries: DataFrame, restaurants_silver: DataFrame
) -> DataFrame:
    channel_type = F.upper(F.trim("channel_type"))
    channel_map = F.create_map(*[F.lit(x) for kv in _CHANNEL_TYPES.items() for x in kv])
    chan = latest_per_key(
        channels.select(
            try_cast("channel_id", "int").alias("channel_id"),
            channel_map[channel_type].alias("channel"),
            *_meta(channels),
        ),
        ["channel_id"],
    ).drop(*_meta_names())

    status_map = F.create_map(*[F.lit(x) for kv in _STATUSES.items() for x in kv])
    raw_status = F.upper(F.trim("order_status"))
    cycle = try_cast("order_metric_cycle_time", "double")
    df = bronze.select(
        F.trim("order_id").alias("order_id"),
        (try_cast("store_id", "bigint") + RESTAURANT_ID_OFFSET).alias("restaurant_id"),
        try_cast("channel_id", "int").alias("channel_id"),
        utc_text_to_local("order_moment_created", TIMESTAMP_FORMAT).alias("created_at_local"),
        F.coalesce(status_map[raw_status], F.lower(raw_status)).alias("status"),
        try_cast("order_amount", "decimal(12,2)").alias("total"),
        F.when((cycle > 0) & (cycle <= MAX_DELIVERY_MINUTES), cycle).alias("delivery_minutes"),
        *_meta(bronze),
    )
    df = latest_per_key(df, ["order_id"]).drop(*_meta_names())
    food = restaurants_silver.select("restaurant_id")
    df = (
        df.join(food, "restaurant_id")  # FOOD stores only
        .join(chan, "channel_id", "left")
        .join(_delivery_per_order(deliveries), "order_id", "left")
        .withColumn("source", F.lit(SOURCE))
        .withColumn("promised_minutes", F.lit(None).cast("double"))
    )
    return conform(with_local_parts(df), ORDER_COLUMNS)


def _meta(df: DataFrame) -> list:
    return [F.col(c) for c in _meta_names() if c in df.columns]


def _meta_names() -> list[str]:
    return ["_ingested_at", "_source_file"]
