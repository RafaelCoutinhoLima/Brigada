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
