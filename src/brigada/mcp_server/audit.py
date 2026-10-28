"""Audit log: one ``tool_audit`` row per tool call, success or error.

``run_id`` links audit rows to an agent run. It is resolved per call, in this order:

1. ``_meta["brigada/run_id"]`` on the MCP ``tools/call`` request (how the runtime passes it
   across the stdio process boundary: ``session.call_tool(..., meta={RUN_ID_META_KEY: id})``);
2. the ``current_run_id`` context variable, for in-process callers (``with run_context(id):``).

``restaurant_id`` scope works the same way (``_meta["brigada/restaurant_id"]`` or
``restaurant_context(id)``) and is mandatory: the server rejects any call with a
``restaurant_id`` argument when no scope is bound or when it differs, so injected text can't
steer the model into another restaurant's data.
"""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from psycopg.types.json import Jsonb

from brigada.mcp_server.db import Conn, connect

RUN_ID_META_KEY = "brigada/run_id"
RESTAURANT_ID_META_KEY = "brigada/restaurant_id"
TOOL_USE_ID_META_KEY = "brigada/tool_use_id"

current_run_id: ContextVar[str | None] = ContextVar("brigada_run_id", default=None)
current_restaurant_id: ContextVar[int | None] = ContextVar("brigada_restaurant_id", default=None)


@contextmanager
def restaurant_context(restaurant_id: int | None) -> Iterator[None]:
    """Only allow tool calls for ``restaurant_id`` inside the block."""
    token = current_restaurant_id.set(restaurant_id)
    try:
        yield
    finally:
        current_restaurant_id.reset(token)


@contextmanager
def run_context(run_id: str | None) -> Iterator[None]:
    """Attribute every tool call made inside the block to ``run_id``."""
    token = current_run_id.set(run_id)
    try:
        yield
    finally:
        current_run_id.reset(token)


def record(
    conn: Conn,
    *,
    tool: str,
    args: Mapping[str, Any],
    result: Any = None,
    error: str | None = None,
    run_id: str | None = None,
    approved_by: str | None = None,
    restaurant_id: int | None = None,
    tool_use_id: str | None = None,
) -> int:
    """Insert an audit row on ``conn``; the caller owns the transaction.

    Everything is passed through ``storable`` first: the audit must never fail because of
    what the model sent (a NUL byte in an argument used to make the INSERT fail and leave the
    call with no trace).
    """
    row = conn.execute(
        "INSERT INTO tool_audit (run_id, tool, args, result, error, approved_by, "
        "restaurant_id, tool_use_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            storable(run_id),
            storable(tool),
            Jsonb(storable(dict(args))),
            Jsonb(storable(result)) if result is not None else None,
            storable(error),
            storable(approved_by),
            # Only a real int fits the column; anything else must not make the audit fail.
            restaurant_id
            if isinstance(restaurant_id, int)
            and not isinstance(restaurant_id, bool)
            and -(2**31) <= restaurant_id < 2**31
            else None,
            storable(tool_use_id),
        ),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def storable(value: Any) -> Any:
    """JSON-safe copy that Postgres accepts: NUL (U+0000) becomes a visible ``\\u0000`` and
    anything not JSON-serializable becomes its ``str``."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    plain = json.loads(json.dumps(value, default=str))
    return _strip_nul(plain)


def _strip_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    return value


def record_standalone(**kwargs: Any) -> int:
    """Same as ``record`` on a fresh connection, committed immediately.

    Used for error paths, where the connection that failed may be in an aborted transaction.
    """
    with connect() as conn:
        return record(conn, **kwargs)
