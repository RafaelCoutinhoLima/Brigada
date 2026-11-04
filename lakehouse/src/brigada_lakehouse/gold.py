"""Gold: zero-filled daily aggregates over the conformed silver of every source.

These are what the MCP tools read through the SQL warehouse (src/brigada/lakehouse):

- ``gold_restaurants``: every restaurant, its source, currency and time zone, and the days it
  has data for.
- ``gold_daily_channel_sales``: revenue and orders per day and channel next to the same
  weekday's mean over the previous ``BASELINE_WEEKS`` weeks (sales_vs_baseline).
- ``gold_item_daily_sales``: quantity and revenue per day and dish (item_trends). Only sources
  that record order lines appear.
- ``gold_delivery_hourly``: delivered orders, delivery time, promise and distance per day and
  local hour (delivery_performance).

Revenue counts completed orders only; canceled orders are counted apart.
"""

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

from brigada_lakehouse.common import iso_weekday
from brigada_lakehouse.sources import brigada, delivery_center, zomato

BASELINE_WEEKS = 4
SOURCE_CHANNELS: dict[str, tuple[str, ...]] = {
    brigada.SOURCE: brigada.CHANNELS,
    delivery_center.SOURCE: delivery_center.CHANNELS,
    zomato.SOURCE: zomato.CHANNELS,
}


def _calendar(orders: DataFrame) -> DataFrame:
    """Every local day between each restaurant's first and last order (inclusive)."""
    bounds = orders.groupBy("restaurant_id", "source").agg(
        F.min("local_day").alias("first_day"), F.max("local_day").alias("last_day")
    )
    return bounds.select(
        "restaurant_id", "source", F.explode(F.sequence("first_day", "last_day")).alias("day")
    )


def gold_restaurants(restaurants: DataFrame, orders: DataFrame) -> DataFrame:
    coverage = orders.groupBy("restaurant_id").agg(
        F.min("local_day").alias("data_from"),
        F.max("local_day").alias("data_until"),
        F.count(F.when(F.col("status") == "completed", True)).alias("completed_orders"),
    )
    return restaurants.join(coverage, "restaurant_id", "left").select(
        "restaurant_id",
        "source",
        "source_id",
        "name",
        "segment",
        "city",
        "state",
        "currency",
        "timezone",
        "data_from",
        "data_until",
        F.coalesce("completed_orders", F.lit(0)).cast("bigint").alias("completed_orders"),
    )


def gold_daily_channel_sales(orders: DataFrame) -> DataFrame:
    """Revenue and orders per restaurant, local day and channel, zero-filled over the channels
    of the restaurant's source, next to the mean of the same weekday in the previous
    ``BASELINE_WEEKS`` weeks.

    Because every day of the calendar is present, the 4 rows before a day in the
    (restaurant, channel, weekday) window are exactly the 4 previous same weekdays.
    ``baseline_weeks`` says how many of them exist (fewer at the start of the history); with 0
    the baseline and the change are null, never a made-up 0.
    """
    spark = orders.sparkSession
    channels = spark.createDataFrame(
        [(s, c) for s, chans in SOURCE_CHANNELS.items() for c in chans], ["source", "channel"]
    )
    grid = _calendar(orders).join(channels, "source")
    daily = orders.groupBy("restaurant_id", F.col("local_day").alias("day"), "channel").agg(
        F.count(F.when(F.col("status") == "completed", True)).alias("orders"),
        F.sum(F.when(F.col("status") == "completed", F.col("total"))).alias("revenue"),
        F.count(F.when(F.col("status") == "canceled", True)).alias("canceled_orders"),
    )
    df = (
        grid.join(daily, ["restaurant_id", "day", "channel"], "left")
        .withColumn("orders", F.coalesce("orders", F.lit(0)).cast("int"))
        .withColumn("revenue", F.coalesce("revenue", F.lit(0)).cast("decimal(14,2)"))
        .withColumn("canceled_orders", F.coalesce("canceled_orders", F.lit(0)).cast("int"))
        .withColumn("iso_weekday", iso_weekday(F.col("day")))
    )
    history = (
        Window.partitionBy("restaurant_id", "channel", "iso_weekday")
        .orderBy("day")
        .rowsBetween(-BASELINE_WEEKS, -1)
    )
    has_history = F.col("baseline_weeks") > 0
    df = (
        df.withColumn("baseline_weeks", F.count("revenue").over(history).cast("int"))
        .withColumn(
            "baseline_revenue",
            F.when(has_history, F.round(F.avg("revenue").over(history), 2)).cast("decimal(14,2)"),
        )
        .withColumn(
            "baseline_orders",
            F.when(has_history, F.round(F.avg("orders").over(history), 2)).cast("double"),
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
        "source",
        "day",
        "iso_weekday",
        "channel",
        "orders",
        "revenue",
        "canceled_orders",
        "baseline_weeks",
        "baseline_orders",
        "baseline_revenue",
        "revenue_change_pct",
    )


def gold_item_daily_sales(
    orders: DataFrame, order_items: DataFrame, menu_items: DataFrame
) -> DataFrame:
    """Quantity, revenue and orders per restaurant, local day and menu item, zero-filled over
    every item of the restaurant (an item that stopped selling shows up with 0). Completed
    orders only; restaurants whose source has no order lines do not appear.

    Revenue needs a price on every order line of the restaurant (Zomato records none): when any
    is missing, ``revenue`` is NULL on every row of that restaurant, never a zero or a partial
    sum, and ``qty`` is still exact."""
    completed = orders.where("status = 'completed'").select(
        "order_id", "restaurant_id", "local_day"
    )
    priced = order_items.groupBy("restaurant_id").agg(
        (F.count("unit_price") == F.count("*")).alias("priced")
    )
    sold = (
        order_items.join(completed, ["order_id", "restaurant_id"])
        .groupBy("restaurant_id", F.col("local_day").alias("day"), "menu_item_id")
        .agg(
            F.sum("qty").alias("qty"),
            F.sum(F.col("qty") * F.col("unit_price")).alias("revenue"),
            F.countDistinct("order_id").alias("orders"),
        )
    )
    items = menu_items.select(
        "restaurant_id",
        "menu_item_id",
        F.col("name").alias("item_name"),
        "category",
        "active",
    )
    calendar = _calendar(orders).drop("source")
    grid = calendar.join(items, "restaurant_id").join(priced, "restaurant_id", "left")
    revenue = F.when(F.col("priced"), F.coalesce("revenue", F.lit(0)))
    return grid.join(sold, ["restaurant_id", "day", "menu_item_id"], "left").select(
        "restaurant_id",
        "day",
        "menu_item_id",
        "item_name",
        "category",
        "active",
        F.coalesce("qty", F.lit(0)).cast("int").alias("qty"),
        revenue.cast("decimal(14,2)").alias("revenue"),
        F.coalesce("orders", F.lit(0)).cast("int").alias("orders"),
    )


def gold_delivery_hourly(orders: DataFrame) -> DataFrame:
    """Per restaurant, local day and hour: completed orders known to be delivered (with a
    delivery time or a distance), how many of them have a delivery time, sums and counts of
    delivery minutes, promised minutes and distance (so any grouping can average them
    correctly), and how many arrived after the promise. A source without delivery times
    (Zomato) still shows its orders and distances, with ``timed_orders`` at 0; sources without
    a promise or a distance leave those counts at 0."""
    delivered = orders.where(
        "status = 'completed' AND (delivery_minutes IS NOT NULL OR distance_km IS NOT NULL)"
    )
    late = (F.col("promised_minutes").isNotNull()) & (
        F.col("delivery_minutes") > F.col("promised_minutes")
    )
    return (
        delivered.groupBy(
            "restaurant_id",
            "source",
            F.col("local_day").alias("day"),
            "iso_weekday",
            F.col("local_hour").alias("hour"),
        )
        .agg(
            F.count("*").cast("int").alias("delivered_orders"),
            F.count("delivery_minutes").cast("int").alias("timed_orders"),
            F.sum("delivery_minutes").alias("delivery_minutes_sum"),
            F.sum("promised_minutes").alias("promised_minutes_sum"),
            F.count("promised_minutes").cast("int").alias("promised_count"),
            F.count(F.when(late, True)).cast("int").alias("late_orders"),
            F.sum("distance_km").alias("distance_km_sum"),
            F.count("distance_km").cast("int").alias("distance_count"),
        )
        .na.fill(0.0, ["delivery_minutes_sum", "promised_minutes_sum", "distance_km_sum"])
    )
