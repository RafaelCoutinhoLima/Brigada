"""Helpers shared by every source and by the gold layer.

Time zones: every source ends up with a local wall clock (``created_at_local``, TIMESTAMP_NTZ in
the source's own ``TIMEZONE``: Brazil for Brigada and Delivery Center, India for Zomato). The
conversions below never depend on the Spark session time zone (a Databricks workspace and a
laptop disagree on it): text is parsed as an exact instant (an explicit ``+0000`` offset), epoch
seconds give its wall clock, and ``convert_timezone`` works on NTZ values.

Currency: amounts stay in the source's own currency (``CURRENCY``, ISO 4217) and are never
converted; ``gold_restaurants.currency`` says which one, and the tools return it.
"""

import re

from pyspark.sql import Column, DataFrame, SparkSession, Window
from pyspark.sql import functions as F

# The conformed silver contract every source maps to (see sources/). Gold reads only these.
RESTAURANT_COLUMNS = (
    "restaurant_id",
    "source",
    "source_id",
    "name",
    "segment",
    "city",
    "state",
    "currency",  # ISO 4217 of every amount of the restaurant (BRL, INR)
    "timezone",  # IANA zone of created_at_local
)
ORDER_COLUMNS = (
    "source",
    "order_id",
    "restaurant_id",
    "created_at_local",
    "local_day",
    "local_hour",
    "iso_weekday",
    "channel",
    "status",  # completed | canceled
    "total",
    "delivery_minutes",  # order to delivery; null when unknown or implausible
    "promised_minutes",  # what the customer was promised; null when the source has none
    "distance_km",
    "rating",  # customer rating 1-5; null when not rated or the source has none
)
ORDER_ITEM_COLUMNS = ("source", "order_id", "restaurant_id", "menu_item_id", "qty", "unit_price")
MENU_ITEM_COLUMNS = ("restaurant_id", "menu_item_id", "name", "category", "active")

ORDER_STATUSES = ("completed", "canceled")

# Rules every source's orders must pass (name -> SQL predicate). The pipeline drops failing rows
# and counts them in the event log (``dlt.expect_all_or_drop``); the tests apply the same ones.
ORDER_EXPECTATIONS = {
    "id_present": "order_id IS NOT NULL",
    "restaurant_present": "restaurant_id IS NOT NULL",
    "created_at_parsed": "created_at_local IS NOT NULL",
    "channel_present": "channel IS NOT NULL",
    "known_status": "status IN ('completed', 'canceled')",
    "non_negative_total": "total >= 0",
}
ORDER_ITEM_EXPECTATIONS = {
    "order_present": "order_id IS NOT NULL",
    "item_present": "menu_item_id IS NOT NULL",
    "positive_qty": "qty > 0",
    # NULL price: the source does not record it (Zomato). Revenue per item is then unknown, not 0.
    "non_negative_price": "unit_price IS NULL OR unit_price >= 0",
}
MENU_ITEM_EXPECTATIONS = {
    "id_present": "menu_item_id IS NOT NULL",
    "restaurant_present": "restaurant_id IS NOT NULL",
}


def column_name(header: str) -> str:
    """A raw header as a Delta-safe column name: Delta rejects spaces and ``,;{}()=`` in names,
    and Zomato's headers have them ("Restaurant discount (Flat offs, Freebies & others)").
    Lowercase, every run of other characters becomes one ``_``. Already plain names
    (Delivery Center, Brigada) do not change."""
    return re.sub(r"[^0-9a-z]+", "_", header.lower()).strip("_")


def read_raw(
    spark: SparkSession, raw_path: str, table: str, *, encoding: str = "UTF-8"
) -> DataFrame:
    """Bronze: one raw table (``<raw_path>/<table>/*.csv``), every value a string as it came
    (only the header is made Delta-safe), plus the source file and the read time."""
    raw = (
        spark.read.option("header", True)
        .option("escape", '"')
        .option("multiLine", True)
        .option("encoding", encoding)
        .csv(f"{raw_path}/{table}/")
    )
    names = [column_name(c) for c in raw.columns]
    if len(set(names)) != len(names):
        raise ValueError(f"{table}: headers collide once made Delta-safe: {raw.columns}")
    raw = raw.toDF(*names)
    return raw.withColumn("_source_file", F.col("_metadata.file_path")).withColumn(
        "_ingested_at", F.current_timestamp()
    )


def apply_expectations(df: DataFrame, expectations: dict[str, str]) -> DataFrame:
    """Keep the rows that pass every predicate (what ``expect_all_or_drop`` does)."""
    for predicate in expectations.values():
        df = df.where(F.expr(predicate))
    return df


def try_cast(column: str, sql_type: str) -> Column:
    """Cast that turns a malformed value into NULL. Plain casts raise under ANSI mode (the
    default on Databricks and Spark 4), which would fail the whole pipeline on one bad row
    instead of letting the expectations drop and count it."""
    return F.expr(f"try_cast(`{column}` AS {sql_type})")


def clean_text(column: str) -> Column:
    """Trimmed, inner whitespace collapsed; empty becomes NULL."""
    text = F.regexp_replace(F.trim(F.col(column)), r"\s+", " ")
    return F.when(text == "", None).otherwise(text)


# Names that reach the model through the tools (dishes, restaurants) come from external files.
# Letters of any script, digits, spaces and basic punctuation, up to 80 characters: no markup,
# no long payloads. Anything else becomes NULL and the expectations drop and count the row.
# \A and \z, not ^ and $: in Java regex $ also matches before a trailing line break.
SAFE_NAME = r"\A[\p{L}\p{N} &'()+./-]{1,80}\z"


def safe_name(name: Column) -> Column:
    """``name`` when it matches ``SAFE_NAME``, else NULL. Shrinks what an external file can put
    in front of the model; it does not make the text trusted (words can still be words)."""
    return F.when(name.rlike(SAFE_NAME), name)


def text_to_wall_clock(column: str, fmt: str) -> Column:
    """A timestamp written as text in ``fmt`` -> the same wall clock as TIMESTAMP_NTZ, whatever
    the session zone. Malformed or impossible values (2/30, 25:61) become NULL.

    The ``+0000`` offset makes the parse an exact instant; its epoch seconds, added to the NTZ
    epoch, give back the written wall clock without passing through the session zone.
    """
    instant = F.expr(f"try_to_timestamp(concat(`{column}`, ' +0000'), '{fmt} Z')")
    return F.expr("timestamp_ntz'1970-01-01 00:00:00'") + F.make_dt_interval(
        F.lit(0), F.lit(0), F.lit(0), F.unix_seconds(instant).cast("decimal(18,6)")
    )


def utc_text_to_local(column: str, fmt: str, tz: str) -> Column:
    """A UTC timestamp written as text in ``fmt`` -> wall clock in ``tz`` (TIMESTAMP_NTZ)."""
    return utc_ntz_to_local(text_to_wall_clock(column, fmt), tz)


def utc_ntz_to_local(utc: Column, tz: str) -> Column:
    return F.convert_timezone(F.lit("UTC"), F.lit(tz), utc)


def iso_weekday(day: Column) -> Column:
    # dayofweek: 1 = Sunday ... 7 = Saturday. ISO: 1 = Monday ... 7 = Sunday.
    return ((F.dayofweek(day) + 5) % 7) + 1


def with_local_parts(df: DataFrame) -> DataFrame:
    """Add local_day, local_hour and iso_weekday from created_at_local."""
    return (
        df.withColumn("local_day", F.to_date("created_at_local"))
        .withColumn("local_hour", F.hour("created_at_local"))
        .withColumn("iso_weekday", iso_weekday(F.col("local_day")))
    )


def latest_per_key(df: DataFrame, keys: list[str]) -> DataFrame:
    """One row per key: the last one read (re-uploads may repeat rows)."""
    if "_ingested_at" not in df.columns:
        return df.dropDuplicates(keys)
    w = Window.partitionBy(*keys).orderBy(F.col("_ingested_at").desc())
    return df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")


def conform(df: DataFrame, columns: tuple[str, ...]) -> DataFrame:
    """Exactly the contract's columns, in order (fails loudly if a source forgot one)."""
    return df.select(*columns)
