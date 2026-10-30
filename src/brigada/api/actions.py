"""Human approval of the actions the agents proposed: list, approve, reject.

The model can only create a pending action (``mcp_server.pending``); these routes are how the
owner decides on it from the console or any HTTP client. Every route is scoped to the API
key's restaurants, and an action outside that scope answers 404, exactly like a missing one.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from brigada.api.auth import get_allowed_restaurants, require_restaurant
from brigada.mcp_server import actions
from brigada.mcp_server.errors import InvalidStateError, NotFoundError
from brigada.mcp_server.models import PendingAction

router = APIRouter(prefix="/actions", tags=["actions"])


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decided_by: str = Field(min_length=1, max_length=100, description="Recorded in the audit.")
    note: str | None = Field(default=None, max_length=500)


@router.get("", response_model=list[PendingAction])
def list_pending(
    allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
    restaurant_id: Annotated[int | None, Query(gt=0)] = None,
    status_: Annotated[
        Literal["pending", "executed", "rejected", "all"], Query(alias="status")
    ] = "pending",
) -> list[PendingAction]:
    scope = allowed
    if restaurant_id is not None:
        require_restaurant(restaurant_id, allowed)
        scope = frozenset({restaurant_id})
    return actions.list_actions(None if status_ == "all" else status_, restaurant_ids=scope)


Decide = Callable[..., PendingAction]


def _decide(
    decide: Decide, action_id: int, body: DecisionRequest, allowed: frozenset[int]
) -> PendingAction:
    try:
        return decide(action_id, body.decided_by, body.note, restaurant_ids=allowed)
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
) -> PendingAction:
    """Apply the proposed change. 404 outside the key's scope, 409 if already decided."""
    return _decide(actions.approve, action_id, body, allowed)


@router.post("/{action_id}/reject", response_model=PendingAction)
def reject(
    action_id: Annotated[int, Path(gt=0)],
    body: DecisionRequest,
    allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
) -> PendingAction:
    """Discard the proposed change without applying it."""
    return _decide(actions.reject, action_id, body, allowed)
