"""Medallion transforms of the Brigada lakehouse, as plain PySpark functions.

The pipeline (``lakehouse/src/pipeline.py``) only wires these functions to Lakeflow
Declarative Pipelines tables; keeping the logic here lets the tests run it on a local
SparkSession (``tests/lakehouse``) without Databricks.

Layers:

- bronze: the CSV files exported by ``scripts/export_lakehouse.py``, every column a string,
  plus where and when each row was read.
- silver: typed, deduplicated rows that pass the data quality expectations below. Orders get
  their local calendar day and hour (``LOCAL_TZ``), computed once here so no consumer has to
  remember the time zone.
- gold: zero-filled daily aggregates that the MCP tools read through a SQL warehouse.

Time zones: the export writes ``created_at_utc`` as a UTC wall clock without offset. It is
parsed as ``TIMESTAMP_NTZ`` and converted with ``convert_timezone``, so the result does not
depend on the Spark session time zone (a Databricks workspace and a laptop disagree on it).
"""

from pyspark.sql import Column, DataFrame, SparkSession, Window
from pyspark.sql import functions as F

LOCAL_TZ = "America/Sao_Paulo"
CHANNELS = ("app", "telefone", "balcao")
# Same-weekday history behind the baseline: the 4 previous Mondays for a Monday, and so on.
BASELINE_WEEKS = 4

# Data quality rules, name -> SQL predicate. The pipeline drops failing rows and records the
# counts in the event log (``dlt.expect_all_or_drop``); the tests apply the same predicates.
ORDER_EXPECTATIONS = {
    "id_present": "id IS NOT NULL",
    "restaurant_present": "restaurant_id IS NOT NULL",
    "created_at_parsed": "created_at_utc IS NOT NULL",
    "known_channel": "channel IN ('app', 'telefone', 'balcao')",
    "non_negative_total": "total >= 0",
}
ORDER_ITEM_EXPECTATIONS = {
    "order_present": "order_id IS NOT NULL",
    "item_present": "menu_item_id IS NOT NULL",
    "positive_qty": "qty > 0",
    "non_negative_price": "unit_price >= 0",
}
MENU_ITEM_EXPECTATIONS = {
    "id_present": "id IS NOT NULL",
    "restaurant_present": "restaurant_id IS NOT NULL",
    "positive_price": "price > 0",
}


def read_raw(spark: SparkSession, raw_path: str, table: str) -> DataFrame:
    """Bronze: one exported table (``<raw_path>/<table>/*.csv``), every column a string, plus
    the source file and the read time."""
    raw = (
        spark.read.option("header", True)
        .option("escape", '"')
        .option("multiLine", True)
        .csv(f"{raw_path}/{table}/")
    )
    return raw.withColumn("_source_file", F.col("_metadata.file_path")).withColumn(
        "_ingested_at", F.current_timestamp()
    )


def apply_expectations(df: DataFrame, expectations: dict[str, str]) -> DataFrame:
    """Keep the rows that pass every predicate (what ``expect_all_or_drop`` does)."""
    for predicate in expectations.values():
        df = df.where(F.expr(predicate))
    return df


def _latest_per_key(df: DataFrame, keys: list[str]) -> DataFrame:
    """One row per key: the last one read (re-exports may repeat rows)."""
    if "_ingested_at" not in df.columns:
        return df.dropDuplicates(keys)
    w = Window.partitionBy(*keys).orderBy(F.col("_ingested_at").desc())
    return df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")


def _try_cast(column: str, sql_type: str) -> Column:
    """Cast that turns a malformed value into NULL. Plain casts raise under ANSI mode (the
    default on Databricks and Spark 4), which would fail the whole pipeline on one bad row
    instead of letting the expectations drop and count it."""
    return F.expr(f"try_cast(`{column}` AS {sql_type})")


def _bool(c: Column) -> Column:
    return F.lower(F.trim(c)).isin("true", "t", "1")


def silver_restaurants(bronze: DataFrame) -> DataFrame:
    df = bronze.select(
        _try_cast("id", "int").alias("id"),
        F.trim("name").alias("name"),
        F.trim("cuisine").alias("cuisine"),
        F.trim("city").alias("city"),
        *_meta(bronze),
    ).where("id IS NOT NULL")
    return _latest_per_key(df, ["id"]).drop(*_meta_names())


def silver_menu_items(bronze: DataFrame) -> DataFrame:
    df = bronze.select(
        _try_cast("id", "int").alias("id"),
        _try_cast("restaurant_id", "int").alias("restaurant_id"),
        F.trim("name").alias("name"),
        F.trim("category").alias("category"),
        _try_cast("price", "decimal(10,2)").alias("price"),
        _bool(F.col("active")).alias("active"),
        *_meta(bronze),
    )
    return _latest_per_key(df, ["id"]).drop(*_meta_names())


def silver_orders(bronze: DataFrame) -> DataFrame:
    """Typed orders with their local day, hour and ISO weekday (1 = Monday ... 7 = Sunday)."""
    utc = _try_cast("created_at_utc", "timestamp_ntz")
    local = F.convert_timezone(F.lit("UTC"), F.lit(LOCAL_TZ), utc)
    df = bronze.select(
        _try_cast("id", "bigint").alias("id"),
        _try_cast("restaurant_id", "int").alias("restaurant_id"),
        utc.alias("created_at_utc"),
        local.alias("created_at_local"),
        F.to_date(local).alias("local_day"),
        F.hour(local).alias("local_hour"),
        F.lower(F.trim("channel")).alias("channel"),
        _try_cast("total", "decimal(10,2)").alias("total"),
        *_meta(bronze),
    )
    df = df.withColumn("iso_weekday", _iso_weekday(F.col("local_day")))
    return _latest_per_key(df, ["id"]).drop(*_meta_names())


def silver_order_items(bronze: DataFrame) -> DataFrame:
    df = bronze.select(
        _try_cast("order_id", "bigint").alias("order_id"),
        _try_cast("menu_item_id", "int").alias("menu_item_id"),
        _try_cast("qty", "int").alias("qty"),
        _try_cast("unit_price", "decimal(10,2)").alias("unit_price"),
        *_meta(bronze),
    )
    return _latest_per_key(df, ["order_id", "menu_item_id"]).drop(*_meta_names())


def _iso_weekday(day: Column) -> Column:
    # dayofweek: 1 = Sunday ... 7 = Saturday. ISO: 1 = Monday ... 7 = Sunday.
    return ((F.dayofweek(day) + 5) % 7) + 1


def _meta(df: DataFrame) -> list[Column]:
    return [F.col(c) for c in _meta_names() if c in df.columns]


def _meta_names() -> list[str]:
    return ["_ingested_at", "_source_file"]


def _restaurant_calendar(orders: DataFrame) -> DataFrame:
    """Every local day between each restaurant's first and last order (inclusive)."""
    bounds = orders.groupBy("restaurant_id").agg(
        F.min("local_day").alias("first_day"), F.max("local_day").alias("last_day")
    )
    return bounds.select(
        "restaurant_id", F.explode(F.sequence("first_day", "last_day")).alias("day")
    )


def gold_daily_channel_sales(orders: DataFrame) -> DataFrame:
    """Revenue and orders per restaurant, local day and channel, zero-filled, next to the
    mean of the same weekday in the previous ``BASELINE_WEEKS`` weeks.

    Because every day of the calendar is present, the 4 rows before a day in the
    (restaurant, channel, weekday) window are exactly the 4 previous same weekdays.
    ``baseline_weeks`` says how many of them exist (fewer at the start of the history);
    with 0 the baseline and the change are null, never a made-up 0.
    """
    channels = orders.sparkSession.createDataFrame([(c,) for c in CHANNELS], ["channel"])
    grid = _restaurant_calendar(orders).crossJoin(channels)
    daily = orders.groupBy("restaurant_id", F.col("local_day").alias("day"), "channel").agg(
        F.count("*").alias("orders"), F.sum("total").alias("revenue")
    )
    df = (
        grid.join(daily, ["restaurant_id", "day", "channel"], "left")
        .withColumn("orders", F.coalesce("orders", F.lit(0)).cast("int"))
        .withColumn("revenue", F.coalesce("revenue", F.lit(0)).cast("decimal(12,2)"))
        .withColumn("iso_weekday", _iso_weekday(F.col("day")))
    )
    history = (
        Window.partitionBy("restaurant_id", "channel", "iso_weekday")
        .orderBy("day")
        .rowsBetween(-BASELINE_WEEKS, -1)
    )
    df = (
        df.withColumn("baseline_weeks", F.count("revenue").over(history).cast("int"))
        .withColumn(
            "baseline_revenue",
            F.when(F.col("baseline_weeks") > 0, F.round(F.avg("revenue").over(history), 2)).cast(
                "decimal(12,2)"
            ),
        )
        .withColumn(
            "baseline_orders",
            F.when(F.col("baseline_weeks") > 0, F.round(F.avg("orders").over(history), 2)).cast(
                "double"
            ),
        )
    )
    change = F.when(
        F.col("baseline_revenue") > 0,
        F.round(
            (F.col("revenue") - F.col("baseline_revenue")) / F.col("baseline_revenue") * 100, 1
        ),
    )
    return df.withColumn("revenue_change_pct", change.cast("double")).select(
        "restaurant_id",
        "day",
        "iso_weekday",
        "channel",
        "orders",
        "revenue",
        "baseline_weeks",
        "baseline_orders",
        "baseline_revenue",
        "revenue_change_pct",
    )


def gold_item_daily_sales(
    orders: DataFrame, order_items: DataFrame, menu_items: DataFrame
) -> DataFrame:
    """Quantity, revenue and orders per restaurant, local day and menu item, zero-filled
    over every item of the restaurant (an item that stopped selling shows up with 0)."""
    sold = (
        order_items.join(
            orders.select(F.col("id").alias("order_id"), "restaurant_id", "local_day"),
            "order_id",
        )
        .groupBy("restaurant_id", F.col("local_day").alias("day"), "menu_item_id")
        .agg(
            F.sum("qty").alias("qty"),
            F.sum(F.col("qty") * F.col("unit_price")).alias("revenue"),
            F.countDistinct("order_id").alias("orders"),
        )
    )
    items = menu_items.select(
        F.col("id").alias("menu_item_id"),
        "restaurant_id",
        F.col("name").alias("item_name"),
        "category",
        "active",
    )
    grid = _restaurant_calendar(orders).join(items, "restaurant_id")
    return grid.join(sold, ["restaurant_id", "day", "menu_item_id"], "left").select(
        "restaurant_id",
        "day",
        "menu_item_id",
        "item_name",
        "category",
        "active",
        F.coalesce("qty", F.lit(0)).cast("int").alias("qty"),
        F.coalesce("revenue", F.lit(0)).cast("decimal(12,2)").alias("revenue"),
        F.coalesce("orders", F.lit(0)).cast("int").alias("orders"),
    )
