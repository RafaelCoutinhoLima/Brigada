"""Zomato: order history of restaurants in Delhi NCR, India (Kaggle, ``order_history_kaggle_data``).

One file, ~21k orders from September 2024 to January 2025 of 21 outlets of 6 brands, with the
dishes of each order, a 1-5 rating and free-text reviews. Amounts are in Indian rupees.

What the profiling of the raw file found, and what this module does about it:

- ``Order Placed At`` ("11:38 PM, September 10 2024") is **India local time**, not UTC: lunch
  peaks at 13h, dinner at 20h and nothing is ordered between 5h and 10h (in UTC the dinner peak
  would be 01:30 in India). Read as the wall clock it is, in ``TIMEZONE``, no conversion. Orders
  after midnight (0h-4h, ~12%) belong to that calendar day, as in every other source.
- ``Items in order`` is ``"1 x Animal Fries, 2 x Salted Fries"``: split on ", ", each part
  ``<qty> x <name>``. No name in the file has a comma. The same dish repeats inside one order
  (80 orders) and with different case ("OG"/"Og Cheese Burger"): lines are summed per
  normalized name. There are **no item prices**: ``unit_price`` is NULL and revenue per dish
  stays unknown (gold and item_trends say so).
- ``menu_item_id`` = a 63-bit hash of (restaurant, normalized name): stable across runs, and
  per restaurant because 190 of the 244 dishes are sold by more than one outlet.
- ``Restaurant ID`` (up to ~21.5 million) becomes ``RESTAURANT_ID_OFFSET + id``, which fits a
  Postgres integer and never meets the Delivery Center's 1,000,000 + store_id. An id that would
  overflow the offset's range becomes NULL and its orders are dropped by an expectation.
- One brand has several outlets ("Aura Pizzas" x5): the name shown is "<brand> (<subzone>)".
  Three ids appear under two subzones: the most frequent one wins. City is "Delhi NCR"; there
  is no state (the region spans three).
- ``Order Status``: Delivered -> completed; Rejected, Returned, Timed out -> canceled. "Picked
  up" and "Return cancelled" (3 each, orders never closed) are left unmapped, so the
  ``known_status`` expectation drops and counts them.
- ``Delivery`` is "Zomato Delivery" in every order: one channel. Any other value is unknown
  and dropped by the ``channel_present`` expectation, never guessed.
- ``Distance`` comes in whole-km bands ("3km") and "<1km", which becomes 0.5 km: a band, not
  missing data, and never 0.
- There is **no delivery time** (``KPT duration`` is kitchen preparation, ``Rider wait time`` is
  the wait at the counter): ``delivery_minutes`` is NULL.
- ``Total`` = subtotal + packaging - discounts (checked on every row): it is the order's
  revenue. No zero and no absurd value in the file, but ``total > 0`` is still an expectation.
- ``Rating`` (1-5, 12% of orders) is kept. ``Review``, ``Instructions`` and complaint tags are
  customer free text, untrusted (ADR 0002), and ``Customer ID`` identifies a person: they stay
  in bronze and never reach silver or any tool.
- Dish, brand, subzone and city names do reach the model (item_trends, gold_restaurants), so
  they must match ``common.SAFE_NAME`` (letters, digits, basic punctuation, up to 80 chars).
  Every name in the real file does; one that does not becomes NULL (a dish line is then
  dropped by ``item_present``; an outlet falls back to "Zomato <id>").
"""

from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F

from brigada_lakehouse.common import (
    MENU_ITEM_COLUMNS,
    ORDER_COLUMNS,
    ORDER_ITEM_COLUMNS,
    RESTAURANT_COLUMNS,
    clean_text,
    conform,
    latest_per_key,
    safe_name,
    text_to_wall_clock,
    try_cast,
    with_local_parts,
)

SOURCE = "zomato"
COUNTRY = "IN"
TIMEZONE = "Asia/Kolkata"
CURRENCY = "INR"
# Brigada restaurant id = offset + Zomato id: below 2^31 and above Delivery Center's range.
RESTAURANT_ID_OFFSET = 100_000_000
MAX_SOURCE_ID = 99_999_999
CHANNELS = ("zomato_delivery",)
TABLES = ("orders",)
# The file as downloaded; scripts/upload_source.py uploads it as the "orders" table.
FILE_NAMES = {"orders": "order_history_kaggle_data"}
TIMESTAMP_FORMAT = "hh:mm a, MMMM d yyyy"
MAX_DISTANCE_KM = 50

ORDER_EXPECTATIONS = {
    "positive_total": "total > 0",
}
_STATUSES = {
    "DELIVERED": "completed",
    "REJECTED": "canceled",
    "RETURNED": "canceled",
    "TIMED OUT": "canceled",
}
_CHANNELS = {"ZOMATO DELIVERY": "zomato_delivery"}
_ITEM = r"^(\d+) x (.+)$"


def _restaurant_id() -> Column:
    raw = try_cast("restaurant_id", "bigint")
    return F.when(raw.between(1, MAX_SOURCE_ID), raw + RESTAURANT_ID_OFFSET)


def _most_frequent(df: DataFrame, key: str, column: str) -> DataFrame:
    """key -> its most frequent non-null value of ``column`` (ties: alphabetical)."""
    counts = df.where(F.col(column).isNotNull()).groupBy(key, column).count()
    w = Window.partitionBy(key).orderBy(F.col("count").desc(), F.col(column).asc())
    return counts.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").select(key, column)


def restaurants(bronze: DataFrame) -> DataFrame:
    """One row per outlet, from the orders file (there is no restaurants file)."""
    df = bronze.select(
        _restaurant_id().alias("restaurant_id"),
        safe_name(clean_text("restaurant_name")).alias("brand"),
        safe_name(clean_text("subzone")).alias("subzone"),
        safe_name(clean_text("city")).alias("city"),
    ).where("restaurant_id IS NOT NULL")
    ids = df.select(
        "restaurant_id",
        (F.col("restaurant_id") - RESTAURANT_ID_OFFSET).cast("string").alias("source_id"),
    ).distinct()
    for column in ("brand", "subzone", "city"):
        ids = ids.join(_most_frequent(df, "restaurant_id", column), "restaurant_id", "left")
    name = F.when(
        F.col("subzone").isNotNull(), F.concat("brand", F.lit(" ("), "subzone", F.lit(")"))
    ).otherwise(F.col("brand"))
    out = ids.select(
        "restaurant_id",
        F.lit(SOURCE).alias("source"),
        "source_id",
        F.coalesce(name, F.concat(F.lit("Zomato "), F.col("source_id"))).alias("name"),
        F.lit("food").alias("segment"),
        "city",
        F.lit(None).cast("string").alias("state"),
        F.lit(COUNTRY).alias("country"),
        F.lit(CURRENCY).alias("currency"),
        F.lit(TIMEZONE).alias("timezone"),
    )
    return conform(out, RESTAURANT_COLUMNS)


def _distance_km(column: str) -> Column:
    text = F.lower(F.regexp_replace(F.col(column), r"\s+", ""))
    band = r"^(\d{1,4}(\.\d{1,3})?)km$"  # the regex makes the cast safe under ANSI
    km = F.when(text == "<1km", F.lit(0.5)).when(
        text.rlike(band), F.regexp_extract(text, band, 1).cast("double")
    )
    return F.when((km > 0) & (km <= MAX_DISTANCE_KM), km)


def orders(bronze: DataFrame) -> DataFrame:
    status_map = F.create_map(*[F.lit(x) for kv in _STATUSES.items() for x in kv])
    raw_status = F.upper(F.trim("order_status"))
    channel_map = F.create_map(*[F.lit(x) for kv in _CHANNELS.items() for x in kv])
    rating = try_cast("rating", "double")
    df = bronze.select(
        F.trim("order_id").alias("order_id"),
        _restaurant_id().alias("restaurant_id"),
        text_to_wall_clock("order_placed_at", TIMESTAMP_FORMAT).alias("created_at_local"),
        channel_map[F.upper(F.trim("delivery"))].alias("channel"),
        F.coalesce(status_map[raw_status], F.lower(raw_status)).alias("status"),
        try_cast("total", "decimal(12,2)").alias("total"),
        _distance_km("distance").alias("distance_km"),
        F.when(rating.between(1, 5), rating).alias("rating"),
        *_meta(bronze),
    )
    df = (
        latest_per_key(df, ["order_id"])
        .drop(*_meta_names())
        .withColumn("source", F.lit(SOURCE))
        .withColumn("delivery_minutes", F.lit(None).cast("double"))
        .withColumn("promised_minutes", F.lit(None).cast("double"))
    )
    return conform(with_local_parts(df), ORDER_COLUMNS)


def _lines(bronze: DataFrame) -> DataFrame:
    """One row per (order, dish) with the summed qty and the dish's spelling in that order.
    A part that is not "<qty> x <name>" keeps a NULL qty or id, for the expectations."""
    deduped = latest_per_key(
        bronze.select(
            F.trim("order_id").alias("order_id"),
            _restaurant_id().alias("restaurant_id"),
            F.col("items_in_order"),
            *_meta(bronze),
        ),
        ["order_id"],
    ).drop(*_meta_names())
    parts = deduped.select(
        "order_id",
        "restaurant_id",
        F.explode(F.split(F.col("items_in_order"), ", ")).alias("part"),
    )
    part = F.trim("part")
    name = F.regexp_replace(F.trim(F.regexp_extract(part, _ITEM, 2)), r"\s+", " ")
    name = safe_name(name)  # also turns "" into NULL
    normalized = F.lower(name)
    item_id = F.when(
        name.isNotNull() & F.col("restaurant_id").isNotNull(),
        F.xxhash64(F.col("restaurant_id"), normalized).bitwiseAND(F.lit(0x7FFFFFFFFFFFFFFF)),
    )
    qty = F.regexp_extract(part, _ITEM, 1)
    return parts.select(
        "order_id",
        "restaurant_id",
        item_id.alias("menu_item_id"),
        name.alias("name"),
        # 1 to 6 digits: the cast cannot overflow, so it cannot fail under ANSI.
        F.when(qty.rlike(r"^\d{1,6}$"), qty.cast("int")).alias("qty"),
    )


def order_items(bronze: DataFrame, orders_silver: DataFrame) -> DataFrame:
    """Order lines of the orders that passed silver. No price in the source: NULL."""
    lines = (
        _lines(bronze)
        .groupBy("order_id", "menu_item_id")
        .agg(F.sum("qty").cast("int").alias("qty"))
    )
    owners = orders_silver.select("order_id", "restaurant_id")
    df = (
        lines.join(owners, "order_id")
        .withColumn("source", F.lit(SOURCE))
        .withColumn("unit_price", F.lit(None).cast("decimal(10,2)"))
    )
    return conform(df, ORDER_ITEM_COLUMNS)


def menu_items(bronze: DataFrame) -> DataFrame:
    """Every dish ever ordered, per outlet, under its most frequent spelling. The source has no
    menu: category and whether it is still on sale are unknown (NULL)."""
    lines = _lines(bronze).where("menu_item_id IS NOT NULL")
    names = _most_frequent(lines, "menu_item_id", "name")
    ids = lines.select("restaurant_id", "menu_item_id").distinct()
    df = ids.join(names, "menu_item_id").select(
        "restaurant_id",
        "menu_item_id",
        "name",
        F.lit(None).cast("string").alias("category"),
        F.lit(None).cast("boolean").alias("active"),
    )
    return conform(df, MENU_ITEM_COLUMNS)


def _meta(df: DataFrame) -> list:
    return [F.col(c) for c in _meta_names() if c in df.columns]


def _meta_names() -> list[str]:
    return ["_ingested_at", "_source_file"]
