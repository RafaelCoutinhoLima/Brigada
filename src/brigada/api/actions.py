"""Human approval of the actions the agents proposed: list, approve, reject.

The model can only create a pending action (``mcp_server.pending``); these routes are how the
owner decides on it from the console or any HTTP client. Every route is scoped to the API
key's restaurants, and an action outside that scope answers 404, exactly like a missing one.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from brigada.api.auth import get_allowed_restaurants, get_key_id, require_restaurant
from brigada.mcp_server import actions
from brigada.mcp_server.db import connect
from brigada.mcp_server.errors import InvalidStateError, NotFoundError
from brigada.mcp_server.models import PendingAction

router = APIRouter(prefix="/actions", tags=["actions"])


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decided_by: str = Field(
        min_length=1,
        max_length=100,
        description="Display name recorded in the audit, next to the id of the API key used.",
    )
    note: str | None = Field(default=None, max_length=500)


class ActionView(PendingAction):
    subject: str | None = Field(description="Name of the dish or ingredient the action is about.")
    unit: str | None = Field(description="The ingredient's unit, for purchases (kg, un, l).")


def _with_subjects(items: list[PendingAction]) -> list[ActionView]:
    """Resolve the names the owner reads, inside each action's own restaurant."""
    dishes = {(a.restaurant_id, a.payload.get("menu_item_id")) for a in items}
    ingredients = {(a.restaurant_id, a.payload.get("ingredient_id")) for a in items}
    names: dict[tuple[str, int, object], tuple[str, str | None]] = {}
    with connect() as conn:
        for table, unit, keys in (
            ("menu_items", "NULL", dishes),
            ("ingredients", "unit", ingredients),
        ):
            pairs = [(r, i) for r, i in keys if isinstance(i, int)]
            if not pairs:
                continue
            rows = conn.execute(
                f"SELECT restaurant_id, id, name, {unit} AS unit FROM {table} "  # noqa: S608 - fixed names
                "WHERE (restaurant_id, id) IN (SELECT * FROM unnest(%s::bigint[], %s::bigint[]))",
                ([r for r, _ in pairs], [i for _, i in pairs]),
            ).fetchall()
            names.update(
                {(table, r["restaurant_id"], r["id"]): (r["name"], r["unit"]) for r in rows}
            )
    out = []
    for a in items:
        table, key = (
            ("ingredients", "ingredient_id")
            if "ingredient_id" in a.payload
            else ("menu_items", "menu_item_id")
        )
        name, unit = names.get((table, a.restaurant_id, a.payload.get(key)), (None, None))
        out.append(ActionView(**a.model_dump(), subject=name, unit=unit))
    return out


@router.get("", response_model=list[ActionView])
def list_pending(
    allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
    restaurant_id: Annotated[int | None, Query(gt=0)] = None,
    status_: Annotated[
        Literal["pending", "executed", "rejected", "all"], Query(alias="status")
    ] = "pending",
) -> list[ActionView]:
    scope = allowed
    if restaurant_id is not None:
        require_restaurant(restaurant_id, allowed)
        scope = frozenset({restaurant_id})
    found = actions.list_actions(None if status_ == "all" else status_, restaurant_ids=scope)
    return _with_subjects(found)


Decide = Callable[..., PendingAction]


def _decide(
    decide: Decide, action_id: int, body: DecisionRequest, allowed: frozenset[int], key_id: str
) -> PendingAction:
    try:
        return decide(
            action_id, body.decided_by, body.note, restaurant_ids=allowed, principal=key_id
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValidationError as exc:
        # The stored payload no longer validates; the details stay in the audit, not here.
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="the stored action is invalid and was not applied"
        ) from exc


@router.post("/{action_id}/approve", response_model=PendingAction)
def approve(
    action_id: Annotated[int, Path(gt=0)],
    body: DecisionRequest,
    allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
    key_id: Annotated[str, Depends(get_key_id)],
) -> PendingAction:
    """Apply the proposed change. 404 outside the key's scope, 409 if already decided."""
    return _decide(actions.approve, action_id, body, allowed, key_id)


@router.post("/{action_id}/reject", response_model=PendingAction)
def reject(
    action_id: Annotated[int, Path(gt=0)],
    body: DecisionRequest,
    allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
    key_id: Annotated[str, Depends(get_key_id)],
) -> PendingAction:
    """Discard the proposed change without applying it."""
    return _decide(actions.reject, action_id, body, allowed, key_id)
