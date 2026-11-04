"""Shared Spark fixtures. pyspark is imported inside the fixtures so the tests that do not need
Spark (test_queries) still run where it is not installed (`uv sync --group spark` adds it)."""

import csv
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts.export_lakehouse import main as export_main

END = date(2026, 10, 27)  # a Tuesday: the planted outage day

# A small Delivery Center extract, written in the raw files' own format and with the defects
# the profiling of the real dataset found (see sources/delivery_center.py).
DC_ORDER_HEADER = (
    "order_id,store_id,channel_id,payment_order_id,delivery_order_id,order_status,order_amount,"
    "order_delivery_fee,order_delivery_cost,order_created_hour,order_created_minute,"
    "order_created_day,order_created_month,order_created_year,order_moment_created,"
    "order_moment_accepted,order_moment_ready,order_moment_collected,"
    "order_moment_in_expedition,order_moment_delivering,order_moment_delivered,"
    "order_moment_finished,order_metric_collected_time,order_metric_paused_time,"
    "order_metric_production_time,order_metric_walking_time,order_metric_expediton_speed_time,"
    "order_metric_transit_time,order_metric_cycle_time"
).split(",")
# order_id, store_id, channel_id, status, amount, created (UTC), cycle_time
DC_ORDERS = [
    ("1", "3", "5", "FINISHED", "50.00", "1/5/2021 12:30:00 AM", "42.5"),  # Mon 21:30 local
    ("2", "3", "1", "CANCELED", "30.00", "1/5/2021 3:00:00 PM", ""),  # Tue 12:00 local
    ("3", "3", "5", "FINISHED", "1788306.00", "1/5/2021 4:00:00 PM", "30"),  # absurd amount
    ("4", "3", "5", "FINISHED", "0", "1/5/2021 4:10:00 PM", "30"),  # zero amount
    ("5", "3", "5", "FINISHED", "80.00", "1/5/2021 11:00:00 PM", "99999"),  # open for days
    ("6", "5", "5", "FINISHED", "70.00", "1/5/2021 11:00:00 PM", "30"),  # GOOD store
    ("7", "3", "5", "FINISHED", "40.00", "2/30/2021 1:00:00 PM", "30"),  # impossible date
    ("8", "3", "99", "FINISHED", "40.00", "1/5/2021 5:00:00 PM", "30"),  # unknown channel
    ("9", "4", "1", "FINISHED", "25.50", "1/5/2021 6:00:00 PM", "35"),
]
DC_DELIVERIES = [  # delivery_id, order_id, driver_id, meters, status
    ("11", "1", "7", "5000", "DELIVERED"),
    ("10", "1", "7", "2000", "DELIVERED"),  # lowest id wins: 2.0 km
    ("12", "5", "8", "7251291", "DELIVERED"),  # 7,251 km: dropped
    ("13", "9", "9", "1500", "CANCELLED"),
]


def write_delivery_center(folder: Path) -> None:
    """Write the extract as the downloaded files (flat folder, Latin-1 where the real ones are)."""
    folder.mkdir(parents=True, exist_ok=True)

    def write(name: str, header: list[str], rows: list[list[str]], enc: str = "utf-8") -> None:
        with (folder / f"{name}.csv").open("w", newline="", encoding=enc) as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)

    write(
        "stores",
        "store_id,hub_id,store_name,store_segment,store_plan_price,store_latitude,store_longitude".split(
            ","
        ),
        [
            ["3", "2", "PAPA  SUCIS ", "FOOD", "0", "", ""],
            ["4", "17", "ÂRPIR PILMACI ", "FOOD", "29.9", "-23.5", "-46.6"],
            ["5", "2", "LOJA DE ROUPA", "GOOD", "49", "", ""],
        ],
        "latin-1",
    )
    write(
        "hubs",
        "hub_id,hub_name,hub_city,hub_state,hub_latitude,hub_longitude".split(","),
        [
            ["2", "BLUE SHOPPING", "PORTO ALEGRE", "RS", "-30.04", "-51.21"],
            ["17", "SMALL SHOPPING", "SÃO PAULO", "SP", "-23.59", "-46.63"],
        ],
        "latin-1",
    )
    write(
        "channels",
        ["channel_id", "channel_name", "channel_type"],
        [["1", "PHONE PLACE", "OWN CHANNEL"], ["5", "FOOD PLACE", "MARKETPLACE"]],
    )
    rows = []
    for oid, store, chan, status, amount, created, cycle in DC_ORDERS:
        row = dict.fromkeys(DC_ORDER_HEADER, "")
        row.update(
            order_id=oid,
            store_id=store,
            channel_id=chan,
            payment_order_id=oid,
            delivery_order_id=oid,
            order_status=status,
            order_amount=amount,
            order_moment_created=created,
            order_metric_cycle_time=cycle,
        )
        rows.append([row[c] for c in DC_ORDER_HEADER])
    write("orders", DC_ORDER_HEADER, rows)
    write(
        "deliveries",
        [
            "delivery_id",
            "delivery_order_id",
            "driver_id",
            "delivery_distance_meters",
            "delivery_status",
        ],
        [list(r) for r in DC_DELIVERIES],
    )


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
    """The raw volume, locally: <dir>/brigada/<table>/ from the export and
    <dir>/delivery_center/<table>/ from upload_source, exactly as in Databricks."""
    from scripts.upload_source import main as upload_main

    out = tmp_path_factory.mktemp("raw")
    argv = ["--source", "seed", "--end-date", END.isoformat(), "--out-dir", str(out)]
    assert export_main(argv) == 0
    downloaded = tmp_path_factory.mktemp("dc_download")
    write_delivery_center(downloaded)
    assert upload_main(["delivery_center", str(downloaded), "--out-dir", str(out)]) == 0
    return out


@pytest.fixture(scope="session")
def tables(spark: Any, raw_dir: Path) -> dict[str, Any]:
    from brigada_lakehouse.build import build

    return build(spark, str(raw_dir), ["brigada", "delivery_center"])


@pytest.fixture(scope="session")
def real_delivery_center_dir() -> Path:
    """The real Kaggle download, when DELIVERY_CENTER_DIR points at it (never in CI)."""
    folder = os.environ.get("DELIVERY_CENTER_DIR")
    if not folder:
        pytest.skip("set DELIVERY_CENTER_DIR to the unzipped Kaggle dataset to run")
    return Path(folder)
