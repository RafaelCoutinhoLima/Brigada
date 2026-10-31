"""Attacks found by the red team review, kept as regression tests."""

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from brigada.mcp_server import audit
from brigada.mcp_server import server as server_module
from brigada.mcp_server.db import Conn
from brigada.mcp_server.server import BrigadaMCPServer
from tests.audit_cleanup import purge_audit

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
    with pytest.raises(ToolError, match="NUL characters"):
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


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("evil\ud800tool", {"restaurant_id": 1}),  # lone surrogate in the tool name
        ("list_menu_items", {"restaurant_id": 1, "x": "\udfff"}),  # in an argument
        ("list_menu_items", {"restaurant_id": 1, "k\ud800": 1}),  # in a key
    ],
)
async def test_lone_surrogates_are_rejected_and_still_audited(
    server: BrigadaMCPServer, db: Conn, run_id: str, name: str, args: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="invalid unicode"):
        await scoped_call(server, name, args)
    [row] = audit_rows(db, run_id)  # Postgres cannot store a surrogate: it is escaped
    assert "invalid unicode" in str(row["error"])
    assert "\\ud800" in f"{row['tool']}{row['args']}" or "\\udfff" in str(row["args"])


def test_storable_makes_anything_insertable() -> None:
    assert audit.storable({"a\x00": ["\x00", 1, None]}) == {"a\\u0000": ["\\u0000", 1, None]}
    assert audit.storable(object()) is not None  # falls back to str, never raises
    assert audit.storable("a\ud800b") == "a\\ud800b"
    assert audit.storable({"k\udfff": ["\ud800"]}) == {"k\\udfff": ["\\ud800"]}
    assert audit.storable("ação 😀") == "ação 😀"  # valid text is untouched


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


async def test_unknown_arguments_are_rejected_not_dropped(
    server: BrigadaMCPServer, db: Conn, run_id: str
) -> None:
    with pytest.raises(ToolError, match=r"unknown \['junk'\]"):
        await scoped_call(
            server,
            "pause_menu_item",
            {"restaurant_id": 1, "menu_item_id": 101, "reason": "x", "junk": "ok"},
        )
    assert pending_for(db, run_id) == []
    assert len(audit_rows(db, run_id)) == 1


async def test_registered_tool_without_restaurant_id_fails_closed(
    server: BrigadaMCPServer, run_id: str
) -> None:
    from brigada.mcp_server.audit import restaurant_context

    with restaurant_context(1), pytest.raises(ToolError, match="restaurant_id is required"):
        await server.call_tool("list_menu_items", {})


async def test_tainted_runs_keep_read_tools_and_lose_anything_not_declared_read_only(
    db: Conn, run_id: str
) -> None:
    from mcp import Client

    from brigada.mcp_server.audit import RESTAURANT_ID_META_KEY, RUN_ID_META_KEY, TAINTED_META_KEY
    from brigada.mcp_server.server import build_server

    server = build_server()

    @server.tool()  # no annotations: must be treated as a write
    def new_unannotated_tool(restaurant_id: int) -> str:
        return "changed something"

    meta = {RUN_ID_META_KEY: run_id, RESTAURANT_ID_META_KEY: 1, TAINTED_META_KEY: True}
    async with Client(server) as client:
        read = await client.call_tool("list_menu_items", {"restaurant_id": 1}, meta=meta)
        other = await client.call_tool("new_unannotated_tool", {"restaurant_id": 1}, meta=meta)
    assert not read.is_error
    assert other.is_error and "customer-written text" in str(other.content)


def test_every_read_tool_is_declared_read_only() -> None:
    from brigada.mcp_server.server import build_server
    from brigada.runtime.mcp_tools import WRITE_TOOLS

    for tool in build_server()._tool_manager.list_tools():
        read_only = tool.annotations is not None and tool.annotations.read_only_hint is True
        assert read_only == (tool.name not in WRITE_TOOLS), tool.name


# R1: the taint is also read back from the audit log, so a client that forgets it (a new
# toolbox opened with the same run_id) still cannot write after reading customer text.

PAUSE_101 = {"restaurant_id": 1, "menu_item_id": 101, "reason": "pedido do dono"}


async def call_with_meta(name: str, args: dict[str, Any], meta: dict[str, Any]) -> Any:
    from mcp import Client

    from brigada.mcp_server.server import build_server

    async with Client(build_server()) as client:
        return await client.call_tool(name, args, meta=meta)


def untainted_meta(run_id: str) -> dict[str, Any]:
    return {audit.RUN_ID_META_KEY: run_id, audit.RESTAURANT_ID_META_KEY: 1}


@pytest.mark.parametrize("reader", sorted(audit.UNTRUSTED_OUTPUT_TOOLS))
async def test_write_after_an_audited_untrusted_read_is_refused_without_taint_meta(
    db: Conn, run_id: str, reader: str
) -> None:
    audit.record(db, tool=reader, args={"restaurant_id": 1}, result={}, run_id=run_id)
    out = await call_with_meta("pause_menu_item", PAUSE_101, untainted_meta(run_id))
    assert out.is_error and "customer-written text" in str(out.content)
    assert pending_for(db, run_id) == []
    refused = audit_rows(db, run_id)[-1]
    assert refused["tool"] == "pause_menu_item" and "customer-written text" in refused["error"]


async def test_write_without_untrusted_read_still_passes(db: Conn, run_id: str) -> None:
    # Another standalone run (orchestration_id NULL, like this one) that read reviews must not
    # taint this one: NULL never links two runs.
    other = f"{run_id}-other"
    add_run(db, run_id, None)
    add_run(db, other, None)
    audit.record(db, tool="search_reviews", args={"restaurant_id": 1}, result={}, run_id=other)
    try:
        await call_with_meta("list_menu_items", {"restaurant_id": 1}, untainted_meta(run_id))
        out = await call_with_meta("pause_menu_item", PAUSE_101, untainted_meta(run_id))
    finally:
        purge_audit(db, "run_id = %s", (other,))
        db.execute("DELETE FROM runs WHERE id = ANY(%s)", ([run_id, other],))
    assert not out.is_error, out.content
    assert len(pending_for(db, run_id)) == 1


async def test_write_without_run_id_is_refused(db: Conn) -> None:
    last_audit = db.execute("SELECT coalesce(max(id), 0) AS m FROM tool_audit").fetchone()["m"]
    last_action = db.execute("SELECT coalesce(max(id), 0) AS m FROM pending_actions").fetchone()
    last_action = last_action["m"]
    try:
        out = await call_with_meta("pause_menu_item", PAUSE_101, {audit.RESTAURANT_ID_META_KEY: 1})
        assert out.is_error and "run id" in str(out.content)
        created = db.execute(
            "SELECT id FROM pending_actions WHERE id > %s AND run_id IS NULL", (last_action,)
        ).fetchall()
        assert created == []
        [row] = db.execute(
            "SELECT tool, error FROM tool_audit WHERE id > %s AND run_id IS NULL", (last_audit,)
        ).fetchall()
        assert row["tool"] == "pause_menu_item" and "run id" in row["error"]
    finally:
        purge_audit(db, "run_id IS NULL AND id > %s", (last_audit,))
        db.execute("DELETE FROM pending_actions WHERE run_id IS NULL AND id > %s", (last_action,))


async def test_database_error_in_the_taint_check_refuses_the_write(
    db: Conn, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(run_id: str) -> bool:
        raise psycopg.OperationalError('connection to server at "10.0.0.5" failed')

    monkeypatch.setattr(server_module, "_read_untrusted_text", broken)
    out = await call_with_meta("pause_menu_item", PAUSE_101, untainted_meta(run_id))
    assert out.is_error and "could not check" in str(out.content)
    assert "10.0.0.5" not in str(out.content)
    assert pending_for(db, run_id) == []


@pytest.fixture
def orchestration(db: Conn, run_id: str) -> Iterator[str]:
    """An orchestration holding this test's run; removed (with its runs) afterwards."""
    oid = f"{run_id}-orch"
    db.execute(
        "INSERT INTO orchestrations (id, restaurant_id, question, today) "
        "VALUES (%s, 1, 'q', '2026-10-27')",
        (oid,),
    )
    add_run(db, run_id, oid)
    yield oid
    purge_audit(db, "run_id LIKE %s", (f"{oid}-%",))
    db.execute("DELETE FROM orchestrations WHERE id = %s", (oid,))


def add_run(db: Conn, run_id: str, orchestration_id: str | None, *, tainted: bool = False) -> None:
    db.execute(
        "INSERT INTO runs (id, agent, restaurant_id, question, today, model, prompt_sha256, "
        "messages, orchestration_id, tainted) "
        "VALUES (%s, 'menu', 1, 'q', '2026-10-27', 'm', 'x', '[]', %s, %s)",
        (run_id, orchestration_id, tainted),
    )


async def test_untrusted_read_by_a_sibling_run_blocks_the_write(
    db: Conn, run_id: str, orchestration: str
) -> None:
    sibling = f"{orchestration}-reviews"
    add_run(db, sibling, orchestration)
    audit.record(db, tool="reviews_for_item", args={"restaurant_id": 1}, result={}, run_id=sibling)
    out = await call_with_meta("pause_menu_item", PAUSE_101, untainted_meta(run_id))
    assert out.is_error and "customer-written text" in str(out.content)
    assert pending_for(db, run_id) == []


@pytest.mark.parametrize("which", ["self", "sibling"])
async def test_runs_tainted_blocks_the_write(
    db: Conn, run_id: str, orchestration: str, which: str
) -> None:
    if which == "self":
        db.execute("UPDATE runs SET tainted = true WHERE id = %s", (run_id,))
    else:
        add_run(db, f"{orchestration}-tainted", orchestration, tainted=True)
    out = await call_with_meta("pause_menu_item", PAUSE_101, untainted_meta(run_id))
    assert out.is_error and "customer-written text" in str(out.content)
    assert pending_for(db, run_id) == []
