"""The whole medallion as plain DataFrames, for local runs and tests.

``pipeline.py`` wires the same functions to Lakeflow tables one by one (what Databricks needs
to track lineage and expectations); this module wires them in one call, applying the
expectations as filters. ``tests/lakehouse/test_pipeline_wiring.py`` checks both define the
same tables.
"""

from pyspark.sql import DataFrame, SparkSession

from brigada_lakehouse import common, gold
from brigada_lakehouse.sources import brigada, delivery_center, zomato

SOURCES = {
    brigada.SOURCE: brigada,
    delivery_center.SOURCE: delivery_center,
    zomato.SOURCE: zomato,
}
WITH_ITEMS = (brigada.SOURCE, zomato.SOURCE)


def build(spark: SparkSession, raw_path: str, sources: list[str]) -> dict[str, DataFrame]:
    """Every table of the pipeline, by its Lakeflow name, for the given sources."""
    t: dict[str, DataFrame] = {}
    for source in sources:
        module = SOURCES[source]
        for table in module.TABLES:
            enc = getattr(module, "ENCODINGS", {}).get(table, "UTF-8")
            t[f"bronze_{source}_{table}"] = common.read_raw(
                spark, f"{raw_path}/{source}", table, encoding=enc
            )

    if brigada.SOURCE in sources:
        b = "bronze_brigada_"
        t["silver_brigada_restaurants"] = brigada.restaurants(t[b + "restaurants"])
        t["silver_brigada_menu_items"] = common.apply_expectations(
            brigada.menu_items(t[b + "menu_items"]), common.MENU_ITEM_EXPECTATIONS
        )
        t["silver_brigada_orders"] = common.apply_expectations(
            brigada.orders(t[b + "orders"], t[b + "deliveries"]), common.ORDER_EXPECTATIONS
        )
        t["silver_brigada_order_items"] = common.apply_expectations(
            brigada.order_items(t[b + "order_items"], t["silver_brigada_orders"]),
            common.ORDER_ITEM_EXPECTATIONS,
        )

    if delivery_center.SOURCE in sources:
        d = "bronze_delivery_center_"
        t["silver_delivery_center_restaurants"] = delivery_center.restaurants(
            t[d + "stores"], t[d + "hubs"]
        )
        t["silver_delivery_center_orders"] = common.apply_expectations(
            delivery_center.orders(
                t[d + "orders"],
                t[d + "channels"],
                t[d + "deliveries"],
                t["silver_delivery_center_restaurants"],
            ),
            {**common.ORDER_EXPECTATIONS, **delivery_center.ORDER_EXPECTATIONS},
        )

    if zomato.SOURCE in sources:
        z = "bronze_zomato_orders"
        t["silver_zomato_restaurants"] = zomato.restaurants(t[z])
        t["silver_zomato_menu_items"] = common.apply_expectations(
            zomato.menu_items(t[z]), common.MENU_ITEM_EXPECTATIONS
        )
        t["silver_zomato_orders"] = common.apply_expectations(
            zomato.orders(t[z]), {**common.ORDER_EXPECTATIONS, **zomato.ORDER_EXPECTATIONS}
        )
        t["silver_zomato_order_items"] = common.apply_expectations(
            zomato.order_items(t[z], t["silver_zomato_orders"]), common.ORDER_ITEM_EXPECTATIONS
        )

    def union(name: str, among: list[str]) -> DataFrame:
        frames = [t[f"silver_{s}_{name}"] for s in among]
        out = frames[0]
        for f in frames[1:]:
            out = out.unionByName(f)
        return out

    with_items = [s for s in sources if s in WITH_ITEMS]
    t["silver_restaurants"] = union("restaurants", sources)
    t["silver_orders"] = union("orders", sources)
    if with_items:
        t["silver_order_items"] = union("order_items", with_items)
        t["silver_menu_items"] = union("menu_items", with_items)

    t["gold_restaurants"] = gold.gold_restaurants(t["silver_restaurants"], t["silver_orders"])
    t["gold_daily_channel_sales"] = gold.gold_daily_channel_sales(
        t["silver_orders"], t["silver_restaurants"]
    )
    if with_items:
        t["gold_item_daily_sales"] = gold.gold_item_daily_sales(
            t["silver_orders"], t["silver_order_items"], t["silver_menu_items"]
        )
    t["gold_delivery_hourly"] = gold.gold_delivery_hourly(t["silver_orders"])
    return t
