"""Human confirmation path for write actions. Deliberately NOT exposed as MCP tools.

The model can only *propose* a change (``pending.py``); a person executes it here:

    uv run python -m brigada.mcp_server.actions list [--status pending|executed|rejected|all]
    uv run python -m brigada.mcp_server.actions approve ACTION_ID --by NAME
    uv run python -m brigada.mcp_server.actions reject ACTION_ID --by NAME [--note TEXT]

``approve`` locks the action row, re-validates the stored payload, applies the change, marks
the action ``executed`` and writes a ``tool_audit`` row with ``approved_by``, all in one
transaction. Failures are audited on a separate connection and re-raised.
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import ValidationError

from brigada.mcp_server import audit
from brigada.mcp_server.db import Conn, connect
from brigada.mcp_server.errors import DomainError, InvalidStateError, NotFoundError
from brigada.mcp_server.models import (
    ActionStatus,
    CreatePromotionInput,
    PauseMenuItemInput,
    PendingAction,
    ProposePurchaseInput,
)
from brigada.mcp_server.queries import get_menu_item

_SELECT = (
    "SELECT id, restaurant_id, kind, payload, reason, status, run_id, created_at, "
    "decided_by, decided_at, decision_note FROM pending_actions"
)


def _to_model(row: dict[str, Any]) -> PendingAction:
    return PendingAction(action_id=row["id"], **{k: v for k, v in row.items() if k != "id"})


def _clean_actor(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("the approver/rejecter name must not be empty")
    return name


def _lock_pending(conn: Conn, action_id: int, scope: frozenset[int] | None) -> PendingAction:
    row = conn.execute(f"{_SELECT} WHERE id = %s FOR UPDATE", (action_id,)).fetchone()
    # Out of scope looks exactly like missing: a caller cannot probe other restaurants' ids.
    if row is None or (scope is not None and row["restaurant_id"] not in scope):
        raise NotFoundError(f"pending action {action_id} does not exist")
    action = _to_model(row)
    if action.status != "pending":
        raise InvalidStateError(f"action {action_id} is already {action.status}")
    return action


def _execute(conn: Conn, action: PendingAction) -> dict[str, Any]:
    """Apply the change. Re-validates the stored payload: it is never trusted blindly."""
    base = {"restaurant_id": action.restaurant_id, "reason": action.reason, **action.payload}
    if action.kind == "pause_menu_item":
        pause = PauseMenuItemInput.model_validate(base)
        get_menu_item(conn, pause.restaurant_id, pause.menu_item_id)  # tenant guard
        conn.execute(
            "UPDATE menu_items SET active = false WHERE id = %s AND restaurant_id = %s",
            (pause.menu_item_id, pause.restaurant_id),
        )
        return {"menu_item_id": pause.menu_item_id, "active": False}
    if action.kind == "propose_purchase":
        from brigada.mcp_server.inventory import get_ingredient

        purchase = ProposePurchaseInput.model_validate(base)
        ing = get_ingredient(conn, purchase.restaurant_id, purchase.ingredient_id)  # tenant guard
        # Approval authorizes the order; stock only changes when the goods are received.
        return {"purchase_approved": {"ingredient": ing["name"], "qty": purchase.qty}}
    if action.kind != "create_promotion":
        raise InvalidStateError(f"unknown action kind {action.kind!r}")
    promo = CreatePromotionInput.model_validate(base)
    get_menu_item(conn, promo.restaurant_id, promo.menu_item_id)  # tenant guard
    row = conn.execute(
        "INSERT INTO promotions (restaurant_id, menu_item_id, discount_pct, start_date, "
        "end_date, reason, source_action_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            promo.restaurant_id,
            promo.menu_item_id,
            promo.discount_pct,
            promo.start_date,
            promo.end_date,
            promo.reason,
            action.action_id,
        ),
    ).fetchone()
    assert row is not None
    return {"promotion_id": int(row["id"])}


def _decide(
    tool: str,
    action_id: int,
    actor: str,
    note: str | None,
    apply: Callable[[Conn, PendingAction], dict[str, Any] | None],
    new_status: ActionStatus,
    scope: frozenset[int] | None,
) -> PendingAction:
    actor = _clean_actor(actor)
    args = {"action_id": action_id, "note": note}
    try:
        with connect() as conn, conn.transaction():
            action = _lock_pending(conn, action_id, scope)
            effect = apply(conn, action)
            row = conn.execute(
                "UPDATE pending_actions SET status = %s, decided_by = %s, decided_at = now(), "
                "decision_note = %s WHERE id = %s RETURNING *",
                (new_status, actor, note, action_id),
            ).fetchone()
            assert row is not None
            decided = _to_model(row)
            audit.record(
                conn,
                tool=tool,
                args=args,
                result={"action": decided.model_dump(mode="json"), "effect": effect},
                run_id=action.run_id,
                approved_by=actor,
                restaurant_id=action.restaurant_id,
            )
            return decided
    except Exception as exc:
        audit.record_standalone(
            tool=tool, args=args, error=f"{type(exc).__name__}: {exc}", approved_by=actor
        )
        raise


def approve(
    action_id: int,
    approved_by: str,
    note: str | None = None,
    *,
    restaurant_ids: frozenset[int] | None = None,
) -> PendingAction:
    """Execute a pending action on behalf of a human. Raises if it is not pending.

    ``restaurant_ids`` limits which restaurants' actions the caller may decide (the HTTP API
    passes the API key's scope); ``None`` is the trusted CLI, which sees everything.
    """
    return _decide(
        "human.approve", action_id, approved_by, note, _execute, "executed", restaurant_ids
    )


def reject(
    action_id: int,
    rejected_by: str,
    note: str | None = None,
    *,
    restaurant_ids: frozenset[int] | None = None,
) -> PendingAction:
    """Reject a pending action without applying it. Raises if it is not pending."""
    return _decide(
        "human.reject",
        action_id,
        rejected_by,
        note,
        lambda _c, _a: None,
        "rejected",
        restaurant_ids,
    )


def list_actions(
    status: ActionStatus | None = "pending", *, restaurant_ids: frozenset[int] | None = None
) -> list[PendingAction]:
    """List actions, oldest first. ``status=None`` lists all; ``restaurant_ids`` limits scope."""
    with connect() as conn:
        rows = conn.execute(
            f"{_SELECT} WHERE (%(s)s::text IS NULL OR status = %(s)s::text) "
            "AND (%(r)s::bigint[] IS NULL OR restaurant_id = ANY(%(r)s::bigint[])) ORDER BY id",
            {"s": status, "r": sorted(restaurant_ids) if restaurant_ids is not None else None},
        ).fetchall()
    return [_to_model(r) for r in rows]


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _print(obj: PendingAction | list[PendingAction]) -> None:
    data = (
        [a.model_dump(mode="json") for a in obj]
        if isinstance(obj, list)
        else obj.model_dump(mode="json")
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m brigada.mcp_server.actions",
        description="Review, approve or reject actions proposed by the agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="list actions")
    p_list.add_argument(
        "--status", choices=["pending", "executed", "rejected", "all"], default="pending"
    )
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a pending action")
        p.add_argument("action_id", type=int)
        p.add_argument("--by", required=True, help="who is deciding (recorded in the audit)")
        p.add_argument("--note", default=None)
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            status: ActionStatus | None = None if args.status == "all" else args.status
            _print(list_actions(status))
        elif args.command == "approve":
            _print(approve(args.action_id, args.by, args.note))
        else:
            _print(reject(args.action_id, args.by, args.note))
    except (DomainError, ValidationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
