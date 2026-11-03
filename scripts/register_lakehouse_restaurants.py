"""Register the real-dataset restaurants of the lakehouse in Postgres (ADR 0009).

Runs, audit rows and API keys reference ``restaurants.id``, so a restaurant must exist in
Postgres before anyone can ask about it. This reads ``gold_restaurants`` through the SQL
warehouse and upserts every restaurant that does not come from Brigada's own synthetic data,
marked ``data_source = 'lakehouse'`` (its orders stay in the lakehouse; the Postgres tools
refuse it and point at the lakehouse tools).

Only rows already marked as lakehouse are ever updated: a Brigada restaurant is never touched.

Usage::

    uv run --extra lakehouse python scripts/register_lakehouse_restaurants.py --min-orders 500
    # then allow an API key to query them, e.g. API_KEYS=dev-key=1,2,3,1000053

Needs DATABASE_URL and the DATABRICKS_* settings (see .env.example).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

type Row = dict[str, Any]

UPSERT = """
INSERT INTO restaurants (id, name, cuisine, city, data_source, lakehouse_source)
VALUES (%(id)s, %(name)s, %(cuisine)s, %(city)s, 'lakehouse', %(source)s)
ON CONFLICT (id) DO UPDATE
SET name = EXCLUDED.name, cuisine = EXCLUDED.cuisine, city = EXCLUDED.city,
    lakehouse_source = EXCLUDED.lakehouse_source
WHERE restaurants.data_source = 'lakehouse'
"""


def to_registry_rows(gold_rows: Sequence[Row], min_orders: int) -> list[Row]:
    """gold_restaurants rows -> restaurants rows. Skips Brigada's own and the small ones."""
    out = []
    for r in gold_rows:
        if r["source"] == "brigada" or int(r["completed_orders"] or 0) < min_orders:
            continue
        city = r["city"] or "?"
        if r.get("state"):
            city = f"{city}/{r['state']}"
        out.append(
            {
                "id": int(r["restaurant_id"]),
                "name": str(r["name"]),
                "cuisine": f"{r['segment'] or 'food'} ({r['source']})",
                "city": city,
                "source": str(r["source"]),
            }
        )
    return out


def register(database_url: str, rows: Sequence[Row]) -> int:
    import psycopg

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.executemany(UPSERT, list(rows))
    return len(rows)


def gold_restaurants() -> list[Row]:
    from brigada.config import get_settings
    from brigada.lakehouse.warehouse import connect, gold_tables

    settings = get_settings()
    if not settings.lakehouse_enabled:
        raise SystemExit("error: DATABRICKS_SERVER_HOSTNAME, _HTTP_PATH and _TOKEN must be set")
    table = gold_tables(settings).restaurants
    with connect(settings) as fetch:
        return fetch(
            "SELECT restaurant_id, source, name, segment, city, state, completed_orders "
            f"FROM {table} ORDER BY completed_orders DESC",
            {},
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register lakehouse restaurants in Postgres.")
    parser.add_argument(
        "--min-orders", type=int, default=100, help="skip restaurants with fewer completed orders"
    )
    parser.add_argument("--dry-run", action="store_true", help="print, do not write")
    args = parser.parse_args(argv)

    from brigada.config import get_settings

    rows = to_registry_rows(gold_restaurants(), args.min_orders)
    for r in rows[:20]:
        print(f"{r['id']}\t{r['name']}\t{r['city']}\t{r['source']}")
    if len(rows) > 20:
        print(f"... and {len(rows) - 20} more")
    if args.dry_run:
        return 0
    n = register(get_settings().database_url, rows)
    print(f"registered {n} restaurants (data_source=lakehouse)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
