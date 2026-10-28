"""Attacks found by the red team review, kept as regression tests."""

from typing import Any

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from brigada.mcp_server import audit
from brigada.mcp_server import server as server_module
from brigada.mcp_server.db import Conn
from brigada.mcp_server.server import BrigadaMCPServer

from .conftest import audit_rows, scoped_call

pytestmark = pytest.mark.db


def pending_for(db: Conn, run_id: str) -> list[dict[str, Any]]:
    return db.execute("SELECT id FROM pending_actions WHERE run_id = %s", (run_id,)).fetchall()


@pytest.mark.parametrize(
    "args",
    [
        {"menu_item_id": 101, "reason": "legit", "junk": "\x00"},  # NUL in an extra argument
        {"menu_item_id": 101, "reason": "ok\x00x"},  # NUL in a real argument
        {"menu_item_id": 101, "reason": "ok", "k\x00ey": 1},  # NUL in a key
    ],
)
async def test_nul_byte_is_rejected_and_still_audited(
    server: BrigadaMCPServer, db: Conn, run_id: str, args: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="NUL characters are not allowed"):
        await scoped_call(server, "pause_menu_item", {"restaurant_id": 1, **args})
    assert pending_for(db, run_id) == []  # nothing was proposed
    [row] = audit_rows(db, run_id)  # and the attempt left a trace
    assert row["tool"] == "pause_menu_item"
    assert "NUL" in str(row["error"])
    assert "\\u0000" in str(row["args"])


async def test_nul_in_tool_name_is_audited(server: BrigadaMCPServer, db: Conn, run_id: str) -> None:
    with pytest.raises(ToolError):
        await scoped_call(server, "evil\x00tool", {"restaurant_id": 1})
    [row] = audit_rows(db, run_id)
    assert row["tool"] == "evil\\u0000tool"


def test_storable_makes_anything_insertable() -> None:
    assert audit.storable({"a\x00": ["\x00", 1, None]}) == {"a\\u0000": ["\\u0000", 1, None]}
    assert audit.storable(object()) is not None  # falls back to str, never raises


async def test_database_errors_do_not_reach_the_model(
    server: BrigadaMCPServer, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Conn:
        raise psycopg.OperationalError('connection to server at "10.0.0.5", user "admin" failed')

    monkeypatch.setattr(server_module, "connect", broken)
    with pytest.raises(ToolError) as info:
        await scoped_call(server, "list_menu_items", {"restaurant_id": 1})
    assert str(info.value).endswith("internal database error; the call was not completed")
    assert "10.0.0.5" not in str(info.value) and "admin" not in str(info.value)


async def test_audit_failure_does_not_mask_the_real_error(
    server: BrigadaMCPServer, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def audit_down(**kwargs: Any) -> int:
        raise psycopg.OperationalError("audit db down at 10.0.0.9")

    monkeypatch.setattr(audit, "record_standalone", audit_down)
    with pytest.raises(ToolError) as info:
        await scoped_call(server, "list_menu_items", {"restaurant_id": 999})
    assert "999" in str(info.value)  # the real validation error, not the audit failure
    assert "10.0.0.9" not in str(info.value)


async def test_unaudited_success_is_not_handed_out(
    server: BrigadaMCPServer, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        audit, "record_standalone", lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
    )
    with pytest.raises(ToolError, match="could not be audited"):
        await scoped_call(server, "list_menu_items", {"restaurant_id": 1})


async def test_audit_records_trusted_restaurant_and_tool_use_id(db: Conn, run_id: str) -> None:
    from mcp import Client

    from brigada.mcp_server.audit import (
        RESTAURANT_ID_META_KEY,
        RUN_ID_META_KEY,
        TOOL_USE_ID_META_KEY,
    )
    from brigada.mcp_server.server import build_server

    meta = {RUN_ID_META_KEY: run_id, RESTAURANT_ID_META_KEY: 1, TOOL_USE_ID_META_KEY: "toolu_9"}
    async with Client(build_server()) as client:
        await client.call_tool("list_menu_items", {"restaurant_id": 1}, meta=meta)
        await client.call_tool("list_menu_items", {"restaurant_id": 2}, meta=meta)  # out of scope
    rows = db.execute(
        "SELECT restaurant_id, tool_use_id, error FROM tool_audit WHERE run_id = %s ORDER BY id",
        (run_id,),
    ).fetchall()
    assert [(r["restaurant_id"], r["tool_use_id"]) for r in rows] == [
        (1, "toolu_9"),
        (1, "toolu_9"),
    ]
    assert rows[0]["error"] is None and "scope" in rows[1]["error"]


@pytest.mark.parametrize("scope", ["um", True, 2**40])
async def test_odd_scope_values_never_break_the_audit(
    server: BrigadaMCPServer, db: Conn, run_id: str, scope: object
) -> None:
    from brigada.mcp_server.audit import restaurant_context

    with restaurant_context(scope), pytest.raises(ToolError):  # type: ignore[arg-type]
        await server.call_tool("list_menu_items", {"restaurant_id": 1})
    [row] = audit_rows(db, run_id)
    assert row["error"] is not None
