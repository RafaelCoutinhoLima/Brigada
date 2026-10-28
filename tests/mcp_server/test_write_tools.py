"""Write tools only create pending actions; the human path in ``actions`` executes them."""

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from brigada.mcp_server import actions
from brigada.mcp_server.db import Conn
from brigada.mcp_server.errors import InvalidStateError, NotFoundError
from brigada.mcp_server.server import BrigadaMCPServer

from .conftest import audit_rows, menu_item_id, scoped_call

# run_id on every test: audit rows it creates are tagged and cleaned up afterwards.
pytestmark = [pytest.mark.db, pytest.mark.usefixtures("run_id")]


async def propose(server: BrigadaMCPServer, name: str, **args: Any) -> dict[str, Any]:
    result = await scoped_call(server, name, args)
    assert not result.is_error and isinstance(result.structured_content, dict)
    return result.structured_content


def is_active(db: Conn, item_id: int) -> bool:
    row = db.execute("SELECT active FROM menu_items WHERE id = %s", (item_id,)).fetchone()
    assert row is not None
    return bool(row["active"])


def action_row(db: Conn, action_id: int) -> dict[str, Any]:
    row = db.execute("SELECT * FROM pending_actions WHERE id = %s", (action_id,)).fetchone()
    assert row is not None
    return row


def promotions_for(db: Conn, action_id: int) -> list[dict[str, Any]]:
    return db.execute(
        "SELECT * FROM promotions WHERE source_action_id = %s", (action_id,)
    ).fetchall()


async def test_pause_creates_pending_action_and_does_not_touch_menu(
    server: BrigadaMCPServer, db: Conn, run_id: str
) -> None:
    item = menu_item_id(db, 2, "Feijoada da casa")
    out = await propose(
        server, "pause_menu_item", restaurant_id=2, menu_item_id=item, reason="avaliações ruins"
    )
    assert out["status"] == "pending"
    assert "awaiting human confirmation" in out["message"]
    row = action_row(db, out["action_id"])
    assert (row["status"], row["kind"], row["run_id"]) == ("pending", "pause_menu_item", run_id)
    assert row["payload"] == {"menu_item_id": item}
    assert is_active(db, item), "the model's call must not change menu_items"

    audit = audit_rows(db, run_id)
    assert [a["tool"] for a in audit] == ["pause_menu_item"]
    assert audit[0]["result"]["action_id"] == out["action_id"]


async def test_approve_executes_pause_in_one_go(
    server: BrigadaMCPServer, db: Conn, run_id: str
) -> None:
    item = menu_item_id(db, 2, "Feijoada da casa")
    out = await propose(
        server, "pause_menu_item", restaurant_id=2, menu_item_id=item, reason="avaliações ruins"
    )
    decided = actions.approve(out["action_id"], approved_by="rafael")
    assert decided.status == "executed" and decided.decided_by == "rafael"
    assert decided.decided_at is not None
    assert not is_active(db, item)

    approvals = [a for a in audit_rows(db, run_id) if a["tool"] == "human.approve"]
    assert len(approvals) == 1
    assert approvals[0]["approved_by"] == "rafael" and approvals[0]["error"] is None

    with pytest.raises(InvalidStateError, match="already executed"):
        actions.approve(out["action_id"], approved_by="rafael")


async def test_pausing_already_paused_item_is_rejected(
    server: BrigadaMCPServer, db: Conn, run_id: str
) -> None:
    item = menu_item_id(db, 1, menu_name(db, 1))
    first = await propose(
        server, "pause_menu_item", restaurant_id=1, menu_item_id=item, reason="x" * 5
    )
    actions.approve(first["action_id"], approved_by="rafael")
    with pytest.raises(ToolError, match="already paused"):
        await scoped_call(
            server,
            "pause_menu_item",
            {"restaurant_id": 1, "menu_item_id": item, "reason": "de novo"},
        )


def menu_name(db: Conn, restaurant_id: int) -> str:
    row = db.execute(
        "SELECT name FROM menu_items WHERE restaurant_id = %s AND active ORDER BY id LIMIT 1",
        (restaurant_id,),
    ).fetchone()
    assert row is not None
    return str(row["name"])


async def test_reject_leaves_menu_unchanged(
    server: BrigadaMCPServer, db: Conn, run_id: str
) -> None:
    item = menu_item_id(db, 2, "Feijoada da casa")
    out = await propose(
        server, "pause_menu_item", restaurant_id=2, menu_item_id=item, reason="avaliações ruins"
    )
    decided = actions.reject(out["action_id"], rejected_by="rafael", note="vou falar com a cozinha")
    assert decided.status == "rejected" and decided.decision_note == "vou falar com a cozinha"
    assert is_active(db, item)
    with pytest.raises(InvalidStateError, match="already rejected"):
        actions.approve(out["action_id"], approved_by="rafael")
    # The failed approval is audited too.
    failures = db.execute(
        "SELECT error FROM tool_audit WHERE tool = 'human.approve' "
        "AND (args->>'action_id')::bigint = %s",
        (out["action_id"],),
    ).fetchall()
    assert len(failures) == 1 and "already rejected" in failures[0]["error"]


async def test_promotion_pending_until_approved(
    server: BrigadaMCPServer, db: Conn, run_id: str
) -> None:
    item = menu_item_id(db, 2, "Feijoada da casa")
    out = await propose(
        server,
        "create_promotion",
        restaurant_id=2,
        menu_item_id=item,
        discount_pct=20,
        start_date="2026-11-01",
        end_date="2026-11-07",
        reason="recuperar a feijoada",
    )
    assert out["kind"] == "create_promotion" and out["status"] == "pending"
    assert promotions_for(db, out["action_id"]) == []

    decided = actions.approve(out["action_id"], approved_by="rafael")
    assert decided.status == "executed"
    promos = promotions_for(db, out["action_id"])
    assert len(promos) == 1
    assert (promos[0]["menu_item_id"], promos[0]["discount_pct"]) == (item, 20)
    assert promos[0]["start_date"].isoformat() == "2026-11-01"


@pytest.mark.parametrize("pct", [0, 51, 90])
async def test_promotion_discount_out_of_bounds_creates_nothing(
    server: BrigadaMCPServer, db: Conn, run_id: str, pct: int
) -> None:
    item = menu_item_id(db, 2, "Feijoada da casa")
    with pytest.raises(ToolError, match="discount_pct"):
        await scoped_call(
            server,
            "create_promotion",
            {
                "restaurant_id": 2,
                "menu_item_id": item,
                "discount_pct": pct,
                "start_date": "2026-11-01",
                "end_date": "2026-11-07",
                "reason": "promoção suspeita",
            },
        )
    assert (
        db.execute("SELECT 1 FROM pending_actions WHERE run_id = %s", (run_id,)).fetchone() is None
    )
    assert audit_rows(db, run_id)[0]["error"] is not None


async def test_write_tools_enforce_tenant(server: BrigadaMCPServer, db: Conn, run_id: str) -> None:
    feijoada = menu_item_id(db, 2, "Feijoada da casa")
    with pytest.raises(ToolError, match="does not exist in restaurant 1"):
        await scoped_call(
            server,
            "pause_menu_item",
            {"restaurant_id": 1, "menu_item_id": feijoada, "reason": "teste"},
        )
    with pytest.raises(ToolError, match="restaurant 999 does not exist"):
        await scoped_call(
            server,
            "pause_menu_item",
            {"restaurant_id": 999, "menu_item_id": feijoada, "reason": "teste"},
        )
    assert (
        db.execute("SELECT 1 FROM pending_actions WHERE run_id = %s", (run_id,)).fetchone() is None
    )


def test_approve_unknown_action(db: Conn) -> None:
    missing = 10**12
    try:
        with pytest.raises(NotFoundError):
            actions.approve(missing, approved_by="rafael")
    finally:
        db.execute(
            "DELETE FROM tool_audit WHERE tool = 'human.approve' "
            "AND (args->>'action_id')::bigint = %s",
            (missing,),
        )


def test_approver_name_required() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        actions.approve(1, approved_by="   ")


async def test_cli_list_and_approve(
    server: BrigadaMCPServer, db: Conn, run_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    item = menu_item_id(db, 2, "Feijoada da casa")
    out = await propose(
        server, "pause_menu_item", restaurant_id=2, menu_item_id=item, reason="via cli"
    )
    assert actions.main(["list"]) == 0
    assert f'"action_id": {out["action_id"]}' in capsys.readouterr().out
    assert actions.main(["approve", str(out["action_id"]), "--by", "rafael"]) == 0
    assert not is_active(db, item)
    assert actions.main(["reject", str(out["action_id"]), "--by", "rafael"]) == 1
    assert "already executed" in capsys.readouterr().err
