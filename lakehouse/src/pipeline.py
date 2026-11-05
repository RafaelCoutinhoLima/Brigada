# Databricks notebook source
# Brigada lakehouse: medallion pipeline (Lakeflow Declarative Pipelines, serverless).
#
# Wiring only: the logic is in brigada_lakehouse/ (sources/*.py map each raw source to one
# conformed silver contract, gold.py aggregates it), tested locally with PySpark
# (tests/lakehouse). Deployed by the bundle in lakehouse/databricks.yml, which sets:
#   bundle.sourcePath -> this folder, so the brigada_lakehouse package can be imported
#   brigada.raw_path  -> the Unity Catalog volume the raw files are uploaded to
#   brigada.sources   -> which sources to build ("brigada,delivery_center,zomato")
#
# bronze_<source>_<table>  raw CSV as uploaded, every column a string, + source file, read time
# silver_<source>_*        typed, cleaned, mapped to the conformed contract, expectations
# silver_*                 the union of every enabled source
# gold_*                   zero-filled daily aggregates read by the MCP tools

import sys

import dlt
from pyspark.sql import DataFrame

sys.path.append(spark.conf.get("bundle.sourcePath", "."))  # noqa: F821 (Databricks global)

from brigada_lakehouse import build, common, gold  # noqa: E402
from brigada_lakehouse.sources import brigada, delivery_center, zomato  # noqa: E402

RAW_PATH = spark.conf.get("brigada.raw_path")  # noqa: F821
SOURCES = [s.strip() for s in spark.conf.get("brigada.sources", "brigada").split(",") if s]  # noqa: F821
KNOWN = build.SOURCES
unknown = set(SOURCES) - set(KNOWN)
if unknown:
    raise ValueError(f"unknown sources in brigada.sources: {sorted(unknown)}")


def _bronze(source: str, table: str, encoding: str = "UTF-8") -> None:
    @dlt.table(name=f"bronze_{source}_{table}", comment=f"{source}/{table} as uploaded.")
    def _t() -> DataFrame:
        return common.read_raw(spark, f"{RAW_PATH}/{source}", table, encoding=encoding)  # noqa: F821


for _source in SOURCES:
    module = KNOWN[_source]
    for _table in module.TABLES:
        _bronze(_source, _table, getattr(module, "ENCODINGS", {}).get(_table, "UTF-8"))


# Silver per source ------------------------------------------------------------------------

if brigada.SOURCE in SOURCES:

    @dlt.table(comment="Brigada synthetic restaurants (conformed).")
    def silver_brigada_restaurants() -> DataFrame:
        return brigada.restaurants(dlt.read("bronze_brigada_restaurants"))

    @dlt.table(comment="Brigada synthetic menu items (conformed).")
    @dlt.expect_all_or_drop(common.MENU_ITEM_EXPECTATIONS)
    def silver_brigada_menu_items() -> DataFrame:
        return brigada.menu_items(dlt.read("bronze_brigada_menu_items"))

    @dlt.table(comment="Brigada synthetic orders with delivery times (conformed).")
    @dlt.expect_all_or_drop(common.ORDER_EXPECTATIONS)
    def silver_brigada_orders() -> DataFrame:
        return brigada.orders(
            dlt.read("bronze_brigada_orders"), dlt.read("bronze_brigada_deliveries")
        )

    @dlt.table(comment="Brigada synthetic order lines (conformed).")
    @dlt.expect_all_or_drop(common.ORDER_ITEM_EXPECTATIONS)
    def silver_brigada_order_items() -> DataFrame:
        return brigada.order_items(
            dlt.read("bronze_brigada_order_items"), dlt.read("silver_brigada_orders")
        )


if delivery_center.SOURCE in SOURCES:

    @dlt.table(comment="Delivery Center FOOD stores as restaurants (conformed).")
    def silver_delivery_center_restaurants() -> DataFrame:
        return delivery_center.restaurants(
            dlt.read("bronze_delivery_center_stores"), dlt.read("bronze_delivery_center_hubs")
        )

    @dlt.table(comment="Delivery Center orders of FOOD stores, UTC -> local (conformed).")
    @dlt.expect_all_or_drop({**common.ORDER_EXPECTATIONS, **delivery_center.ORDER_EXPECTATIONS})
    def silver_delivery_center_orders() -> DataFrame:
        return delivery_center.orders(
            dlt.read("bronze_delivery_center_orders"),
            dlt.read("bronze_delivery_center_channels"),
            dlt.read("bronze_delivery_center_deliveries"),
            dlt.read("silver_delivery_center_restaurants"),
        )


if zomato.SOURCE in SOURCES:

    @dlt.table(comment="Zomato outlets (Delhi NCR) as restaurants (conformed).")
    def silver_zomato_restaurants() -> DataFrame:
        return zomato.restaurants(dlt.read("bronze_zomato_orders"))

    @dlt.table(comment="Zomato dishes ever ordered, per outlet; no prices or categories.")
    @dlt.expect_all_or_drop(common.MENU_ITEM_EXPECTATIONS)
    def silver_zomato_menu_items() -> DataFrame:
        return zomato.menu_items(dlt.read("bronze_zomato_orders"))

    @dlt.table(comment="Zomato orders, India local time, rupees; no review text (conformed).")
    @dlt.expect_all_or_drop({**common.ORDER_EXPECTATIONS, **zomato.ORDER_EXPECTATIONS})
    def silver_zomato_orders() -> DataFrame:
        return zomato.orders(dlt.read("bronze_zomato_orders"))

    @dlt.table(comment="Zomato order lines parsed from 'Items in order'; price unknown (NULL).")
    @dlt.expect_all_or_drop(common.ORDER_ITEM_EXPECTATIONS)
    def silver_zomato_order_items() -> DataFrame:
        return zomato.order_items(
            dlt.read("bronze_zomato_orders"), dlt.read("silver_zomato_orders")
        )


# Conformed silver (union of the enabled sources) ------------------------------------------


def _union(name: str, sources: list[str]) -> DataFrame:
    frames = [dlt.read(f"silver_{s}_{name}") for s in sources]
    out = frames[0]
    for f in frames[1:]:
        out = out.unionByName(f)
    return out


WITH_ITEMS = [s for s in SOURCES if s in build.WITH_ITEMS]  # sources that record order lines


@dlt.table(comment="Every restaurant of every enabled source.")
def silver_restaurants() -> DataFrame:
    return _union("restaurants", SOURCES)


@dlt.table(comment="Every order of every enabled source, local time.")
def silver_orders() -> DataFrame:
    return _union("orders", SOURCES)


if WITH_ITEMS:

    @dlt.table(comment="Order lines of the sources that record them.")
    def silver_order_items() -> DataFrame:
        return _union("order_items", WITH_ITEMS)

    @dlt.table(comment="Menu items of the sources that record them.")
    def silver_menu_items() -> DataFrame:
        return _union("menu_items", WITH_ITEMS)


# Gold ---------------------------------------------------------------------------------------


@dlt.table(comment="Restaurants with their source and data coverage.")
def gold_restaurants() -> DataFrame:
    return gold.gold_restaurants(dlt.read("silver_restaurants"), dlt.read("silver_orders"))


@dlt.table(
    comment="Revenue, orders and cancellations per restaurant, local day and channel "
    f"(zero-filled), with the mean of the previous {gold.BASELINE_WEEKS} same weekdays that "
    "were not holidays (holidays flagged). Read by sales_vs_baseline."
)
def gold_daily_channel_sales() -> DataFrame:
    return gold.gold_daily_channel_sales(dlt.read("silver_orders"), dlt.read("silver_restaurants"))


if WITH_ITEMS:

    @dlt.table(
        comment="Quantity, revenue and orders per restaurant, local day and menu item "
        "(zero-filled over every item). Read by item_trends."
    )
    def gold_item_daily_sales() -> DataFrame:
        return gold.gold_item_daily_sales(
            dlt.read("silver_orders"), dlt.read("silver_order_items"), dlt.read("silver_menu_items")
        )


@dlt.table(
    comment="Delivery time, promise, lateness and distance per restaurant, local day and hour. "
    "Read by delivery_performance."
)
def gold_delivery_hourly() -> DataFrame:
    return gold.gold_delivery_hourly(dlt.read("silver_orders"))
