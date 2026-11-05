"""Gold: zero-filled daily aggregates over the conformed silver of every source.

These are what the MCP tools read through the SQL warehouse (src/brigada/lakehouse):

- ``gold_restaurants``: every restaurant, its source, currency and time zone, and the days it
  has data for.
- ``gold_daily_channel_sales``: revenue and orders per day and channel next to the mean of the
  previous ``BASELINE_WEEKS`` same weekdays that were not holidays (sales_vs_baseline).
- ``gold_item_daily_sales``: quantity and revenue per day and dish (item_trends). Only sources
  that record order lines appear.
- ``gold_delivery_hourly``: delivered orders, delivery time, promise and distance per day and
  local hour (delivery_performance).

Revenue counts completed orders only; canceled orders are counted apart.
"""

from collections.abc import Sequence

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

from brigada_lakehouse.common import iso_weekday
from brigada_lakehouse.holidays import HOLIDAYS, Holiday
from brigada_lakehouse.sources import brigada, delivery_center, zomato

BASELINE_WEEKS = 4
# How far back the baseline may reach to find BASELINE_WEEKS normal (non-holiday) days.
BASELINE_LOOKBACK_WEEKS = 8
# Where the delivery time goes (``<phase>_minutes`` in the silver contract).
PHASES = ("prep", "pickup", "transit", "rider_wait")
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
        "country",
        "currency",
        "timezone",
        "data_from",
        "data_until",
        F.coalesce("completed_orders", F.lit(0)).cast("bigint").alias("completed_orders"),
    )


def restaurant_holidays(
    restaurants: DataFrame, holidays: Sequence[Holiday] = HOLIDAYS
) -> DataFrame:
    """(restaurant_id, day, holiday_name) for every holiday that applies to the restaurant:
    same country, and the holiday's state and city (when set) equal to the restaurant's.
    Several holidays on one day are joined with " / "."""
    spark = restaurants.sparkSession
    cal = spark.createDataFrame(
        [(h.country, h.state, h.city, h.day, h.name) for h in holidays],
        "country string, h_state string, h_city string, day date, holiday_name string",
    )
    r = restaurants.select(
        "restaurant_id",
        "country",
        F.upper("state").alias("r_state"),
        F.lower("city").alias("r_city"),
    )
    applies = (
        (F.col("h_state").isNull() | (F.upper("h_state") == F.col("r_state")))
        & (F.col("h_city").isNull() | (F.lower("h_city") == F.col("r_city")))
        # a city holiday needs the state too (the same city name exists in other states)
        & (F.col("h_city").isNull() | F.col("h_state").isNull() | F.col("r_state").isNotNull())
    )
    return (
        r.join(cal, "country")
        .where(applies)
        .groupBy("restaurant_id", "day")
        .agg(F.concat_ws(" / ", F.sort_array(F.collect_set("holiday_name"))).alias("holiday_name"))
    )


def gold_daily_channel_sales(
    orders: DataFrame, restaurants: DataFrame, holidays: Sequence[Holiday] = HOLIDAYS
) -> DataFrame:
    """Revenue and orders per restaurant, local day and channel, zero-filled over the channels
    of the restaurant's source, next to the mean of the previous ``BASELINE_WEEKS`` same
    weekdays that were **not holidays** for that restaurant.

    The baseline looks back at most ``BASELINE_LOOKBACK_WEEKS`` weeks, so a holiday in the way
    is skipped and an older normal day takes its place. A holiday row gets a baseline too (made
    of normal days) and carries ``holiday_name``, so the tool can say "it was a holiday" instead
    of reporting an unexplained drop. ``baseline_weeks`` says how many normal days were found
    (fewer at the start of the history); with 0 the baseline and the change are null, never a
    made-up 0.
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
        .join(restaurant_holidays(restaurants, holidays), ["restaurant_id", "day"], "left")
        .withColumn("orders", F.coalesce("orders", F.lit(0)).cast("int"))
        .withColumn("revenue", F.coalesce("revenue", F.lit(0)).cast("decimal(14,2)"))
        .withColumn("canceled_orders", F.coalesce("canceled_orders", F.lit(0)).cast("int"))
        .withColumn("iso_weekday", iso_weekday(F.col("day")))
        .withColumn("is_holiday", F.col("holiday_name").isNotNull())
    )

    # Every (day, earlier normal same weekday within the lookback), newest first; keep 4.
    normal = df.where(~F.col("is_holiday")).select(
        "restaurant_id",
        "channel",
        F.col("day").alias("past_day"),
        F.col("revenue").alias("past_revenue"),
        F.col("orders").alias("past_orders"),
    )
    target = df.select("restaurant_id", "channel", "day")
    lag_days = F.datediff(F.col("day"), F.col("past_day"))
    pairs = target.join(normal, ["restaurant_id", "channel"]).where(
        (lag_days > 0) & (lag_days % 7 == 0) & (lag_days <= 7 * BASELINE_LOOKBACK_WEEKS)
    )
    newest = Window.partitionBy("restaurant_id", "channel", "day").orderBy(F.col("past_day").desc())
    baseline = (
        pairs.withColumn("_rank", F.row_number().over(newest))
        .where(F.col("_rank") <= BASELINE_WEEKS)
        .groupBy("restaurant_id", "channel", "day")
        .agg(
            F.count("*").cast("int").alias("baseline_weeks"),
            F.round(F.avg("past_revenue"), 2).cast("decimal(14,2)").alias("baseline_revenue"),
            F.round(F.avg("past_orders"), 2).cast("double").alias("baseline_orders"),
        )
    )
    df = df.join(baseline, ["restaurant_id", "channel", "day"], "left").withColumn(
        "baseline_weeks", F.coalesce("baseline_weeks", F.lit(0)).cast("int")
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
        "is_holiday",
        "holiday_name",
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
    correctly), and how many arrived after the promise. The same sums and counts for each phase
    of the time (``PHASES``: kitchen, food waiting for the courier, transit, courier waiting for
    the food), so a slow hour can be blamed on the kitchen or on the delivery. A source without
    delivery times (Zomato) still shows its orders and distances, with ``timed_orders`` at 0;
    sources without a promise, a distance or a phase leave those counts at 0."""
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
            *[
                agg
                for phase in PHASES
                for agg in (
                    F.sum(f"{phase}_minutes").alias(f"{phase}_minutes_sum"),
                    F.count(f"{phase}_minutes").cast("int").alias(f"{phase}_count"),
                )
            ],
        )
        .na.fill(
            0.0,
            ["delivery_minutes_sum", "promised_minutes_sum", "distance_km_sum"]
            + [f"{phase}_minutes_sum" for phase in PHASES],
        )
    )
