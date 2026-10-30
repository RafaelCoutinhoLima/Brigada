"""Purchase orders: an approved purchase is ordered; receiving it adds the goods to stock.

Scoped like ``/actions``: the API key's restaurants only, and an order outside that scope
answers 404 exactly like a missing one.
"""

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field

from brigada.api.auth import get_allowed_restaurants, get_key_id, require_restaurant
from brigada.mcp_server import actions
from brigada.mcp_server.errors import InvalidStateError, NotFoundError
from brigada.mcp_server.models import PurchaseOrder

router = APIRouter(prefix="/purchase-orders", tags=["purchases"])


class ReceiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    received_by: str = Field(min_length=1, max_length=100)
    unit_cost: Decimal = Field(gt=0, le=100_000, decimal_places=2, description="Price paid.")


@router.get("", response_model=list[PurchaseOrder])
def list_orders(
    allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
    restaurant_id: Annotated[int | None, Query(gt=0)] = None,
    status_: Annotated[
        Literal["ordered", "received", "cancelled", "all"], Query(alias="status")
    ] = "ordered",
) -> list[PurchaseOrder]:
    scope = allowed
    if restaurant_id is not None:
        require_restaurant(restaurant_id, allowed)
        scope = frozenset({restaurant_id})
    return actions.list_purchase_orders(None if status_ == "all" else status_, restaurant_ids=scope)


@router.post("/{order_id}/receive", response_model=PurchaseOrder)
def receive(
    order_id: Annotated[int, Path(gt=0)],
    body: ReceiveRequest,
    allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
    key_id: Annotated[str, Depends(get_key_id)],
) -> PurchaseOrder:
    """Register the delivery at the price paid. 404 outside the key's scope, 409 if done."""
    try:
        return actions.receive_purchase(
            order_id, body.received_by, body.unit_cost, restaurant_ids=allowed, principal=key_id
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
