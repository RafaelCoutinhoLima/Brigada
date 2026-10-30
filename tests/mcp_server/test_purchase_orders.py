"""An approved purchase becomes an order; receiving it is what changes the stock."""

from decimal import Decimal
from typing import Any

import pytest

from brigada.mcp_server import actions, inventory
from brigada.mcp_server.db import Conn
from brigada.mcp_server.errors import InvalidStateError, NotFoundError
from brigada.mcp_server.models import StockLevelsInput
from brigada.mcp_server.server import BrigadaMCPServer

from .conftest import scoped_call

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("run_id")]


def mozzarella(db: Conn) -> dict[str, Any]:
    level = next(
        i
        for i in inventory.stock_levels(db, StockLevelsInput(restaurant_id=1)).items
        if i.name == "Mussarela"
    )
    return level.model_dump()


async def approve_purchase(server: BrigadaMCPServer, db: Conn, qty: float) -> dict[str, Any]:
    proposed = await scoped_call(
        server,
        "propose_purchase",
        {
            "restaurant_id": 1,
            "ingredient_id": mozzarella(db)["ingredient_id"],
            "qty": qty,
            "reason": "cobertura menor que o prazo do fornecedor",
        },
    )
    assert isinstance(proposed.structured_content, dict)
    action_id = proposed.structured_content["action_id"]
    actions.approve(action_id, approved_by="gerente")
    (order,) = [
        o
        for o in actions.list_purchase_orders(restaurant_ids=frozenset({1}))
        if o.source_action_id == action_id
    ]
    return order.model_dump()


async def test_approval_places_an_order_that_stops_the_shortage_being_proposed_again(
    server: BrigadaMCPServer, db: Conn
) -> None:
    before = mozzarella(db)
    assert before["below_reorder_point"] and before["on_order"] == 0
    order = await approve_purchase(server, db, qty=20)
    assert (order["status"], order["qty"], order["ingredient"]) == ("ordered", 20, "Mussarela")
    after = mozzarella(db)
    assert after["on_order"] == 20 and not after["below_reorder_point"]
    # The goods have not arrived: what is on the shelf did not change.
    assert after["on_hand"] == before["on_hand"] and after["runs_out_before_delivery"]


async def test_receiving_adds_the_goods_at_the_price_paid(
    server: BrigadaMCPServer, db: Conn
) -> None:
    before = mozzarella(db)
    order = await approve_purchase(server, db, qty=20)
    received = actions.receive_purchase(
        order["purchase_order_id"], "estoquista", Decimal("41.90"), principal="key:test"
    )
    assert (received.status, received.unit_cost, received.received_by) == (
        "received",
        41.9,
        "estoquista",
    )
    after = mozzarella(db)
    assert after["on_hand"] == pytest.approx(before["on_hand"] + 20)
    assert after["on_order"] == 0
    move = db.execute(
        "SELECT m.kind, m.qty, m.unit_cost FROM purchase_orders po "
        "JOIN stock_movements m ON m.id = po.movement_id WHERE po.id = %s",
        (order["purchase_order_id"],),
    ).fetchone()
    assert move == {"kind": "purchase", "qty": Decimal("20.000"), "unit_cost": Decimal("41.90")}
    audit = db.execute(
        "SELECT approved_by, args->>'principal' AS p, restaurant_id FROM tool_audit "
        "WHERE tool = 'human.receive_purchase' AND (args->>'purchase_order_id')::bigint = %s",
        (order["purchase_order_id"],),
    ).fetchall()
    assert audit == [{"approved_by": "estoquista", "p": "key:test", "restaurant_id": 1}]
    with pytest.raises(InvalidStateError, match="already received"):
        actions.receive_purchase(order["purchase_order_id"], "estoquista", Decimal("41.90"))


async def test_another_restaurant_cannot_receive_the_order(
    server: BrigadaMCPServer, db: Conn
) -> None:
    order = await approve_purchase(server, db, qty=5)
    with pytest.raises(NotFoundError, match="does not exist"):
        actions.receive_purchase(
            order["purchase_order_id"], "x", Decimal("10"), restaurant_ids=frozenset({3})
        )
    assert actions.list_purchase_orders(restaurant_ids=frozenset({3})) == []
    still = [
        o
        for o in actions.list_purchase_orders()
        if o.purchase_order_id == order["purchase_order_id"]
    ]
    assert still[0].status == "ordered"


def test_receipt_needs_a_name_and_a_positive_price() -> None:
    with pytest.raises(ValueError):
        actions.receive_purchase(1, "  ", Decimal("1"))
    with pytest.raises(ValueError, match="positive"):
        actions.receive_purchase(1, "x", Decimal("0"))
