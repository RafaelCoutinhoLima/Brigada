"""Inventory tools on states the seed never produces, inside a transaction that is rolled back."""

from collections.abc import Iterator
from datetime import date, timedelta

import pytest

from brigada.mcp_server import inventory
from brigada.mcp_server.db import Conn, connect
from brigada.mcp_server.errors import InvalidStateError
from brigada.mcp_server.models import (
    CmvInput,
    CostChangesInput,
    DishMarginsInput,
    StockLevelsInput,
    WasteReportInput,
)

from .conftest import END_DATE, menu_item_id

pytestmark = pytest.mark.db
LAST30 = {"start_date": END_DATE - timedelta(days=29), "end_date": END_DATE}
NEW_ING, NEW_DISH, NEW_ORDER = 990001, 990002, 990003


@pytest.fixture
def scratch() -> Iterator[Conn]:
    with connect() as conn, conn.transaction(force_rollback=True):
        yield conn


def add_ingredient(conn: Conn, rid: int = 1) -> None:
    conn.execute(
        "INSERT INTO ingredients (id, restaurant_id, name, unit, lead_time_days, safety_days) "
        "VALUES (%s, %s, 'Trufa negra', 'kg', 3, 1)",
        (NEW_ING, rid),
    )


def add_dish(conn: Conn, rid: int = 1, price: int = 50) -> None:
    conn.execute(
        "INSERT INTO menu_items (id, restaurant_id, name, category, price, active) "
        "VALUES (%s, %s, 'Prato novo', 'principal', %s, true)",
        (NEW_DISH, rid, price),
    )


def sell_new_dish(conn: Conn, qty: int = 10, price: int = 50) -> None:
    conn.execute(
        "INSERT INTO orders (id, restaurant_id, created_at, channel, total) "
        "VALUES (%s, 1, %s, 'balcao', %s)",
        (NEW_ORDER, f"{END_DATE - timedelta(days=2)} 13:00-03", qty * price),
    )
    conn.execute(
        "INSERT INTO order_items (order_id, menu_item_id, qty, unit_price) VALUES (%s, %s, %s, %s)",
        (NEW_ORDER, NEW_DISH, qty, price),
    )


def test_unpriced_ingredient_refuses_instead_of_costing_zero(scratch: Conn) -> None:
    add_ingredient(scratch)
    margherita = menu_item_id(scratch, 1, "Pizza margherita")
    scratch.execute(
        "INSERT INTO recipes (restaurant_id, menu_item_id, ingredient_id, qty) "
        "VALUES (1, %s, %s, 5)",
        (margherita, NEW_ING),
    )
    with pytest.raises(InvalidStateError, match="Trufa negra"):
        inventory.dish_margins(scratch, DishMarginsInput(restaurant_id=1, **LAST30))
    with pytest.raises(InvalidStateError, match="Trufa negra"):
        inventory.cmv(scratch, CmvInput(restaurant_id=1, **LAST30))


def test_period_before_the_first_purchase_is_a_clear_error(scratch: Conn) -> None:
    jan = {"start_date": date(2026, 1, 1), "end_date": date(2026, 1, 31)}
    with pytest.raises(InvalidStateError, match="no purchase cost"):
        inventory.dish_margins(scratch, DishMarginsInput(restaurant_id=1, **jan))


def test_dish_without_recipe_is_reported_not_hidden(scratch: Conn) -> None:
    before = inventory.cmv(scratch, CmvInput(restaurant_id=1, **LAST30))
    add_dish(scratch)
    sell_new_dish(scratch, qty=10, price=50)
    after = inventory.cmv(scratch, CmvInput(restaurant_id=1, **LAST30))
    assert after.revenue == pytest.approx(before.revenue + 500, abs=0.01)
    assert after.revenue_without_recipe == pytest.approx(500, abs=0.01)
    assert after.dishes_without_recipe == ["Prato novo"]
    assert after.cost_of_goods == pytest.approx(before.cost_of_goods, abs=0.01)
    assert after.cmv_pct == pytest.approx(before.cmv_pct, abs=1e-4)  # over covered revenue
    margins = inventory.dish_margins(scratch, DishMarginsInput(restaurant_id=1, **LAST30))
    assert margins.dishes_without_recipe == ["Prato novo"]
    assert NEW_DISH not in {m.menu_item_id for m in margins.items}


def test_cost_changes_without_a_previous_window_is_an_error(scratch: Conn) -> None:
    with pytest.raises(InvalidStateError, match="no purchases"):
        inventory.cost_changes(scratch, CostChangesInput(restaurant_id=1, recent_days=90))


def test_stock_without_sales_still_has_a_reference_date(scratch: Conn) -> None:
    scratch.execute("DELETE FROM stock_movements WHERE restaurant_id = 2 AND kind = 'sale'")
    out = inventory.stock_levels(scratch, StockLevelsInput(restaurant_id=2))
    assert out.items and all(i.cover_days is None for i in out.items)


def count_new_ingredient(conn: Conn, expected: float, counted: float) -> None:
    conn.execute(
        "INSERT INTO stock_counts "
        "(id, restaurant_id, ingredient_id, counted_qty, expected_qty, at) "
        "VALUES (%s, 1, %s, %s, %s, %s)",
        (NEW_ING, NEW_ING, counted, expected, f"{END_DATE - timedelta(days=1)} 22:00-03"),
    )


def buy_new_ingredient(conn: Conn, when: date, unit_cost: float, mid: int) -> None:
    conn.execute(
        "INSERT INTO stock_movements (id, restaurant_id, ingredient_id, kind, qty, unit_cost, at) "
        "VALUES (%s, 1, %s, 'purchase', 10, %s, %s)",
        (mid, NEW_ING, unit_cost, f"{when} 08:00-03"),
    )


def test_a_loss_without_a_purchase_price_is_unknown_not_free(scratch: Conn) -> None:
    add_ingredient(scratch)
    count_new_ingredient(scratch, expected=10, counted=8)
    out = inventory.waste_report(scratch, WasteReportInput(restaurant_id=1, **LAST30))
    line = next(i for i in out.items if i.ingredient_id == NEW_ING)
    assert line.loss == 2 and line.loss_value is None
    assert out.uncosted == ["Trufa negra"]
    assert out.total_loss_value == pytest.approx(
        sum(i.loss_value for i in out.items if i.loss_value is not None)
    )


def test_a_loss_is_valued_at_the_periods_purchase_cost(scratch: Conn) -> None:
    """A price from a year ago must not dilute the cost of what was lost this month."""
    add_ingredient(scratch)
    buy_new_ingredient(scratch, END_DATE - timedelta(days=365), 100, NEW_ING)
    buy_new_ingredient(scratch, END_DATE - timedelta(days=10), 300, NEW_ING + 1)
    count_new_ingredient(scratch, expected=10, counted=8)
    out = inventory.waste_report(scratch, WasteReportInput(restaurant_id=1, **LAST30))
    line = next(i for i in out.items if i.ingredient_id == NEW_ING)
    assert line.loss_value == pytest.approx(2 * 300)
