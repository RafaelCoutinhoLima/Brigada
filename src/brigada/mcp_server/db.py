"""Database access helpers.

Connection strategy: one short-lived psycopg connection per tool call (``connect()``).
The MCP server handles one model at a time over stdio, so a pool would add moving parts
without a measurable gain; switching to ``psycopg_pool.ConnectionPool`` later only touches
this module. Every query is parameterized; no SQL text ever comes from the model.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import DictRow, dict_row

from brigada.config import get_settings

type Conn = psycopg.Connection[DictRow]


def connect() -> Conn:
    """Open a new connection with dict rows. Use as a context manager (commits on exit)."""
    return psycopg.connect(get_settings().database_url, row_factory=dict_row)


def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def local_day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Half-open ``[start 00:00, end+1 00:00)`` in local time for inclusive calendar days."""
    tz = local_tz()
    return (
        datetime.combine(start, time.min, tz),
        datetime.combine(end + timedelta(days=1), time.min, tz),
    )
