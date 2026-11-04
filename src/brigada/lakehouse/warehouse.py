"""Connection to a Databricks SQL warehouse (the lakehouse tools' only way to the gold tables).

One short-lived connection per tool call, like ``mcp_server/db.py``: the server answers one
model at a time and a serverless warehouse keeps its own session pool. The driver
(``databricks-sql-connector``, extra ``lakehouse``) is imported lazily so the base install and
the Docker image do not need it.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any

from brigada.config import Settings
from brigada.lakehouse.queries import Fetch, GoldTables, Row


def gold_tables(settings: Settings) -> GoldTables:
    return GoldTables(settings.lakehouse_catalog, settings.lakehouse_schema)


@contextmanager
def connect(settings: Settings) -> Iterator[Fetch]:
    """Yield a ``Fetch`` bound to a new warehouse connection; closed on exit."""
    from databricks import sql  # extra "lakehouse"

    with sql.connect(
        server_hostname=settings.databricks_server_hostname,
        http_path=settings.databricks_http_path,
        access_token=settings.databricks_token,
    ) as conn:

        def fetch(query: str, params: dict[str, Any]) -> list[Row]:
            with conn.cursor() as cur:
                cur.execute(query, params)
                columns = [d[0] for d in cur.description or []]
                return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

        yield fetch


@dataclass(frozen=True)
class Lakehouse:
    """What the MCP server needs to serve the lakehouse tools: a way to open a ``Fetch`` and
    the gold table names. The tests pass one backed by a fake."""

    open: Callable[[], AbstractContextManager[Fetch]]
    tables: GoldTables


def from_settings(settings: Settings) -> Lakehouse | None:
    """The configured warehouse, or None when the lakehouse is off."""
    if not settings.lakehouse_enabled:
        return None
    return Lakehouse(open=lambda: connect(settings), tables=gold_tables(settings))
