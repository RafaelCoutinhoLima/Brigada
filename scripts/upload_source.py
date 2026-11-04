"""Upload a real dataset, as downloaded, to the lakehouse raw volume (ADR 0009).

The files go up untouched: cleaning is the pipeline's job (bronze keeps what arrived, silver
fixes it), so a re-run of the pipeline can always start from the original.

Layout: ``/Volumes/<catalog>/<schema>/raw/<source>/<table>/<table>.csv``.

Sources:

- ``delivery_center``: Kaggle "Delivery Center: Food & Goods orders in Brazil"
  (kaggle.com/datasets/nosbielcs/brazilian-delivery-center). Unzip it and point at the folder
  with orders.csv, stores.csv, hubs.csv, channels.csv and deliveries.csv (payments.csv and
  drivers.csv are not used yet).
- ``zomato``: Kaggle Zomato restaurant order history (Delhi NCR, Sep 2024 to Jan 2025). Unzip it
  and point at the folder with order_history_kaggle_data.csv; it goes up as the orders table.

Usage::

    uv run --extra lakehouse python scripts/upload_source.py delivery_center ~/Downloads/dc
    # lay the files out locally instead (tests, local Spark), no Databricks call
    uv run python scripts/upload_source.py delivery_center ~/Downloads/dc --out-dir data/raw

The data is not committed to this repository: get it from its source and keep its license.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

# Tables each source needs, as file stems. Kept in sync with the pipeline's source modules by
# tests/lakehouse (those import pyspark; this script must not need it).
SOURCES: dict[str, tuple[str, ...]] = {
    "delivery_center": ("orders", "stores", "hubs", "channels", "deliveries"),
    "zomato": ("orders",),
}
# File stem when the download does not name it after the table.
FILE_NAMES: dict[str, dict[str, str]] = {
    "zomato": {"orders": "order_history_kaggle_data"},
}


def find_files(source: str, folder: Path) -> dict[str, Path]:
    """table -> file. Fails listing every missing file, not just the first."""
    tables = SOURCES[source]
    stems = FILE_NAMES.get(source, {})
    found = {t: folder / f"{stems.get(t, t)}.csv" for t in tables}
    missing = [str(p) for p in found.values() if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"{source}: missing {', '.join(missing)}")
    return found


def volume_path(catalog: str, schema: str, source: str, table: str) -> str:
    return f"/Volumes/{catalog}/{schema}/raw/{source}/{table}/{table}.csv"


def upload(source: str, files: dict[str, Path], catalog: str, schema: str) -> dict[str, str]:
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient()
    written = {}
    for table, path in files.items():
        target = volume_path(catalog, schema, source, table)
        with path.open("rb") as fh:
            client.files.upload(target, fh, overwrite=True)
        written[table] = target
    return written


def lay_out(source: str, files: dict[str, Path], out_dir: Path) -> dict[str, str]:
    written = {}
    for table, path in files.items():
        target = out_dir / source / table / f"{table}.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        written[table] = str(target)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload a raw dataset to the lakehouse.")
    parser.add_argument("source", choices=sorted(SOURCES))
    parser.add_argument("folder", type=Path, help="folder with the dataset's CSV files")
    parser.add_argument("--catalog", default=os.environ.get("LAKEHOUSE_CATALOG", "workspace"))
    parser.add_argument("--schema", default=os.environ.get("LAKEHOUSE_SCHEMA", "brigada"))
    parser.add_argument(
        "--out-dir", type=Path, default=None, help="copy here (<dir>/<source>/<table>/) instead"
    )
    args = parser.parse_args(argv)
    try:
        files = find_files(args.source, args.folder)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.out_dir is not None:
        written = lay_out(args.source, files, args.out_dir)
    else:
        written = upload(args.source, files, args.catalog, args.schema)
    for table, target in written.items():
        size = files[table].stat().st_size / 1e6
        print(f"{table}: {size:.1f} MB -> {target}")
    if args.out_dir is None:
        print("next: cd lakehouse && databricks bundle run brigada_refresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
