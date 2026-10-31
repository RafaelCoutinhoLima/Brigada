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
# Set by the runtime once a run has read customer-written text: write tools then refuse.
TAINTED_META_KEY = "brigada/tainted"
# Tools whose output is customer-written text (possible prompt injection). Shared by the
# runtime (taint in the client) and the server (taint read back from this audit log).
UNTRUSTED_OUTPUT_TOOLS = frozenset({"reviews_for_item", "search_reviews"})

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
    """JSON-safe copy that Postgres accepts: NUL (U+0000) becomes a visible ``\\u0000``, a lone
    surrogate a visible ``\\udXXX``, and anything not JSON-serializable becomes its ``str``."""
    if value is None:
        return None
    if isinstance(value, str):
        return _safe_text(value)
    plain = json.loads(json.dumps(value, default=str))
    return _strip_nul(plain)


def _safe_text(text: str) -> str:
    # A lone surrogate is not valid UTF-8 and the JSON round trip keeps it as is: Postgres
    # rejects it in text and in jsonb, so the audit row would fail.
    escaped = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return escaped.replace("\x00", "\\u0000")


def _strip_nul(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    return value


def read_untrusted_text(conn: Conn, run_id: str) -> bool:
    """True if ``run_id``, or any run of the same orchestration, read customer-written text.

    "Read" means an audited call to an ``UNTRUSTED_OUTPUT_TOOLS`` tool, failed calls included
    (a failed read may still carry text), or a run stored with ``runs.tainted``. Siblings are
    matched with plain ``=``: runs without an orchestration (NULL) are never siblings.
    """
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM runs WHERE id = %(rid)s AND tainted)"
        " OR EXISTS (SELECT 1 FROM tool_audit WHERE run_id = %(rid)s AND tool = ANY(%(tools)s))"
        " OR EXISTS ("
        "   SELECT 1 FROM runs me JOIN runs sib ON sib.orchestration_id = me.orchestration_id"
        "   WHERE me.id = %(rid)s AND (sib.tainted OR EXISTS ("
        "     SELECT 1 FROM tool_audit a WHERE a.run_id = sib.id AND a.tool = ANY(%(tools)s)))"
        " ) AS untrusted",
        {"rid": run_id, "tools": sorted(UNTRUSTED_OUTPUT_TOOLS)},
    ).fetchone()
    assert row is not None
    return bool(row["untrusted"])


def record_standalone(**kwargs: Any) -> int:
    """Same as ``record`` on a fresh connection, committed immediately.

    Used for error paths, where the connection that failed may be in an aborted transaction.
    """
    with connect() as conn:
        return record(conn, **kwargs)
