"""Write-tool proposals: the model's write tools land here and only create a pending action.

Nothing in this module changes ``menu_items`` or ``promotions``. Execution lives in
``actions.py`` (human path), which the MCP server never imports.
"""

from typing import Any

from psycopg.types.json import Jsonb

from brigada.mcp_server.db import Conn
from brigada.mcp_server.errors import InvalidStateError
from brigada.mcp_server.models import (
    ActionKind,
    CreatePromotionInput,
    PauseMenuItemInput,
    PendingActionCreated,
)
from brigada.mcp_server.queries import ensure_restaurant, get_menu_item

AWAITING = "awaiting human confirmation; the change has NOT been applied"


def _insert(
    conn: Conn,
    *,
    restaurant_id: int,
    kind: ActionKind,
    payload: dict[str, Any],
    reason: str,
    run_id: str | None,
) -> int:
    row = conn.execute(
        "INSERT INTO pending_actions (restaurant_id, kind, payload, reason, run_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (restaurant_id, kind, Jsonb(payload), reason, run_id),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def propose_pause(
    conn: Conn, inp: PauseMenuItemInput, run_id: str | None = None
) -> PendingActionCreated:
    ensure_restaurant(conn, inp.restaurant_id)
    item = get_menu_item(conn, inp.restaurant_id, inp.menu_item_id)
    if not item.active:
        raise InvalidStateError(f"menu item {item.menu_item_id} ({item.name}) is already paused")
    action_id = _insert(
        conn,
        restaurant_id=inp.restaurant_id,
        kind="pause_menu_item",
        payload={"menu_item_id": inp.menu_item_id},
        reason=inp.reason,
        run_id=run_id,
    )
    return PendingActionCreated(
        action_id=action_id,
        kind="pause_menu_item",
        message=f"Pause of '{item.name}' (id {item.menu_item_id}) is {AWAITING}.",
    )


def propose_promotion(
    conn: Conn, inp: CreatePromotionInput, run_id: str | None = None
) -> PendingActionCreated:
    ensure_restaurant(conn, inp.restaurant_id)
    item = get_menu_item(conn, inp.restaurant_id, inp.menu_item_id)
    action_id = _insert(
        conn,
        restaurant_id=inp.restaurant_id,
        kind="create_promotion",
        payload={
            "menu_item_id": inp.menu_item_id,
            "discount_pct": inp.discount_pct,
            "start_date": inp.start_date.isoformat(),
            "end_date": inp.end_date.isoformat(),
        },
        reason=inp.reason,
        run_id=run_id,
    )
    return PendingActionCreated(
        action_id=action_id,
        kind="create_promotion",
        message=(
            f"Promotion of {inp.discount_pct}% on '{item.name}' (id {item.menu_item_id}) "
            f"from {inp.start_date} to {inp.end_date} is {AWAITING}."
        ),
    )
