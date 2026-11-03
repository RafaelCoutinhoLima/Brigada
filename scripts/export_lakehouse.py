"""Export the sales tables to the Databricks lakehouse (bronze input, ADR 0009).

Writes one CSV per table and uploads it to the Unity Catalog volume the bundle creates
(``/Volumes/<catalog>/<schema>/raw/<table>/<table>.csv``). The pipeline
(``lakehouse/src/pipeline.py``) reads those files as its bronze layer.

Only the tables the gold layer needs leave Postgres: restaurants, menu items, orders and
order lines. Reviews stay out on purpose: they are customer-written text (prompt injection
lives there, ADR 0002) and nothing in the lakehouse needs them yet.

``created_at`` becomes ``created_at_utc``: a UTC wall clock without offset, so Spark parses
it the same way whatever the session time zone (see ``transforms.py``).

Usage::

    # from Postgres (DATABASE_URL), the same data the MCP server reads
    uv run --extra lakehouse python scripts/export_lakehouse.py
    # straight from the generator, no database needed
    uv run --extra lakehouse python scripts/export_lakehouse.py --source seed --end-date 2026-10-27
    # write the CSVs locally and stop (no Databricks call)
    uv run python scripts/export_lakehouse.py --source seed --out-dir data/lakehouse

Authentication for the upload: ``databricks auth login`` (CLI profile) or DATABRICKS_HOST and
DATABRICKS_TOKEN, read by the Databricks SDK.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# Run as `python scripts/export_lakehouse.py`, the repo root is not on sys.path.
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

type Table = tuple[tuple[str, ...], list[tuple[Any, ...]]]

# Exported columns per table, in the order of the CSV header. Fixed here: the export never
# selects "*", so a new Postgres column does not silently reach the lakehouse.
EXPORT_COLUMNS: dict[str, tuple[str, ...]] = {
    "restaurants": ("id", "name", "cuisine", "city"),
    "menu_items": ("id", "restaurant_id", "name", "category", "price", "active"),
    "orders": ("id", "restaurant_id", "created_at", "total", "channel"),
    "order_items": ("order_id", "menu_item_id", "qty", "unit_price"),
}
# CSV header names that differ from the Postgres column.
RENAMED = {"created_at": "created_at_utc"}


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"naive datetime {value!r}: the export needs an aware timestamp")
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def to_csv(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """CSV text with a header; timestamps in UTC without offset, decimals as plain text."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([RENAMED.get(c, c) for c in columns])
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return buf.getvalue()


def tables_from_seed(end_date: date, seed: int) -> dict[str, Table]:
    from scripts.seed_data import _rows, generate

    generated = _rows(generate(end_date, seed=seed))
    out: dict[str, Table] = {}
    for table, wanted in EXPORT_COLUMNS.items():
        columns, rows = generated[table]
        idx = [columns.index(c) for c in wanted]
        out[table] = (wanted, [tuple(r[i] for i in idx) for r in rows])
    return out


def tables_from_db(database_url: str) -> dict[str, Table]:
    import psycopg
    from psycopg import sql

    out: dict[str, Table] = {}
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        for table, columns in EXPORT_COLUMNS.items():
            cur.execute(
                sql.SQL("SELECT {} FROM {}").format(
                    sql.SQL(", ").join(sql.Identifier(c) for c in columns), sql.Identifier(table)
                )
            )
            out[table] = (columns, list(cur.fetchall()))
    return out


def volume_path(catalog: str, schema: str, table: str) -> str:
    return f"/Volumes/{catalog}/{schema}/raw/{table}/{table}.csv"


def upload(tables: dict[str, Table], catalog: str, schema: str) -> dict[str, str]:
    """Upload each table's CSV, replacing the previous export. Returns table -> path."""
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient()
    written: dict[str, str] = {}
    for table, (columns, rows) in tables.items():
        path = volume_path(catalog, schema, table)
        data = to_csv(columns, rows).encode("utf-8")
        client.files.upload(path, io.BytesIO(data), overwrite=True)
        written[table] = path
    return written


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the sales tables to the lakehouse.")
    parser.add_argument(
        "--source",
        choices=("db", "seed"),
        default="db",
        help="db: read DATABASE_URL (default); seed: run the generator, no database",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        help="with --source seed: last day of the period (default: yesterday, local)",
    )
    parser.add_argument("--seed", type=int, default=42, help="with --source seed (default 42)")
    parser.add_argument("--catalog", default=os.environ.get("LAKEHOUSE_CATALOG", "workspace"))
    parser.add_argument("--schema", default=os.environ.get("LAKEHOUSE_SCHEMA", "brigada"))
    parser.add_argument(
        "--out-dir", type=Path, default=None, help="write the CSVs here and skip the upload"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.source == "seed":
        from scripts.seed_data import default_end_date

        tables = tables_from_seed(args.end_date or default_end_date(), args.seed)
    else:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            print("error: DATABASE_URL is not set (or use --source seed)", file=sys.stderr)
            return 2
        tables = tables_from_db(database_url)

    if args.out_dir is not None:
        for table, (columns, rows) in tables.items():
            target = args.out_dir / table / f"{table}.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(to_csv(columns, rows), encoding="utf-8")
            print(f"{table}: {len(rows)} rows -> {target}")
        return 0

    written = upload(tables, args.catalog, args.schema)
    for table, path in written.items():
        print(f"{table}: {len(tables[table][1])} rows -> {path}")
    print("next: cd lakehouse && databricks bundle run brigada_refresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
