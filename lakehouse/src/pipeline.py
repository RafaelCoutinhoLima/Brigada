# Databricks notebook source
# Brigada lakehouse: medallion pipeline (Lakeflow Declarative Pipelines, serverless).
#
# Wiring only: the logic is in brigada_lakehouse/transforms.py, tested locally with PySpark
# (tests/lakehouse). Deployed by the bundle in lakehouse/databricks.yml, which sets:
#   bundle.sourcePath -> this folder, so the transforms module can be imported
#   brigada.raw_path  -> the Unity Catalog volume the export script uploads the CSVs to
#
# bronze_*  raw CSV, every column a string, plus source file and read time
# silver_*  typed, deduplicated, data quality expectations (failing rows are dropped and
#           counted in the pipeline event log)
# gold_*    zero-filled daily aggregates read by the MCP tools through a SQL warehouse

import sys

import dlt
from pyspark.sql import DataFrame

sys.path.append(spark.conf.get("bundle.sourcePath", "."))  # noqa: F821 (Databricks global)

from brigada_lakehouse import transforms as t  # noqa: E402

RAW_PATH = spark.conf.get("brigada.raw_path")  # noqa: F821


def _raw(table: str) -> DataFrame:
    return t.read_raw(spark, RAW_PATH, table)  # noqa: F821


# Bronze ---------------------------------------------------------------------------------


@dlt.table(comment="Restaurants as exported, all strings.")
def bronze_restaurants() -> DataFrame:
    return _raw("restaurants")


@dlt.table(comment="Menu items as exported, all strings.")
def bronze_menu_items() -> DataFrame:
    return _raw("menu_items")


@dlt.table(comment="Orders as exported, all strings; created_at_utc is a UTC wall clock.")
def bronze_orders() -> DataFrame:
    return _raw("orders")


@dlt.table(comment="Order lines as exported, all strings.")
def bronze_order_items() -> DataFrame:
    return _raw("order_items")


# Silver ---------------------------------------------------------------------------------


@dlt.table(comment="Restaurants, typed and deduplicated.")
def silver_restaurants() -> DataFrame:
    return t.silver_restaurants(dlt.read("bronze_restaurants"))


@dlt.table(comment="Menu items, typed and deduplicated.")
@dlt.expect_all_or_drop(t.MENU_ITEM_EXPECTATIONS)
def silver_menu_items() -> DataFrame:
    return t.silver_menu_items(dlt.read("bronze_menu_items"))


@dlt.table(comment=f"Orders with local day, hour and ISO weekday ({t.LOCAL_TZ}).")
@dlt.expect_all_or_drop(t.ORDER_EXPECTATIONS)
def silver_orders() -> DataFrame:
    return t.silver_orders(dlt.read("bronze_orders"))


@dlt.table(comment="Order lines, typed and deduplicated.")
@dlt.expect_all_or_drop(t.ORDER_ITEM_EXPECTATIONS)
def silver_order_items() -> DataFrame:
    return t.silver_order_items(dlt.read("bronze_order_items"))


# Gold -----------------------------------------------------------------------------------


@dlt.table(
    comment="Revenue and orders per restaurant, local day and channel (zero-filled), with "
    f"the mean of the same weekday over the previous {t.BASELINE_WEEKS} weeks. "
    "Read by the sales_vs_baseline MCP tool."
)
def gold_daily_channel_sales() -> DataFrame:
    return t.gold_daily_channel_sales(dlt.read("silver_orders"))


@dlt.table(
    comment="Quantity, revenue and orders per restaurant, local day and menu item "
    "(zero-filled over every item). Read by the item_trends MCP tool."
)
def gold_item_daily_sales() -> DataFrame:
    return t.gold_item_daily_sales(
        dlt.read("silver_orders"), dlt.read("silver_order_items"), dlt.read("silver_menu_items")
    )
