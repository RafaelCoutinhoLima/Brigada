"""Shared Spark fixtures. pyspark is imported inside the fixtures so the tests that do not need
Spark (test_queries) still run where it is not installed (`uv sync --group spark` adds it)."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts.export_lakehouse import main as export_main

END = date(2026, 10, 27)  # a Tuesday: the planted outage day


@pytest.fixture(scope="session")
def spark(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder.master("local[2]")
        .appName("brigada-lakehouse-tests")
        # A session zone far from both UTC and Sao Paulo: results must not depend on it.
        .config("spark.sql.session.timeZone", "Asia/Tokyo")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", str(tmp_path_factory.mktemp("warehouse")))
        .getOrCreate()
    )
    yield session
    session.stop()


@pytest.fixture(scope="session")
def raw_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The seed exported exactly as for the volume: <dir>/<table>/<table>.csv."""
    out = tmp_path_factory.mktemp("raw")
    argv = ["--source", "seed", "--end-date", END.isoformat(), "--out-dir", str(out)]
    assert export_main(argv) == 0
    return out


@pytest.fixture(scope="session")
def silver(spark: Any, raw_dir: Path) -> dict[str, Any]:
    from brigada_lakehouse import transforms as t

    raw = {
        name: t.read_raw(spark, str(raw_dir), name)
        for name in ("restaurants", "menu_items", "orders", "order_items")
    }
    return {
        "orders": t.apply_expectations(t.silver_orders(raw["orders"]), t.ORDER_EXPECTATIONS),
        "order_items": t.apply_expectations(
            t.silver_order_items(raw["order_items"]), t.ORDER_ITEM_EXPECTATIONS
        ),
        "menu_items": t.apply_expectations(
            t.silver_menu_items(raw["menu_items"]), t.MENU_ITEM_EXPECTATIONS
        ),
        "restaurants": t.silver_restaurants(raw["restaurants"]),
    }
