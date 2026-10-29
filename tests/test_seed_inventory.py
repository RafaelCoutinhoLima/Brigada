from datetime import date
from decimal import Decimal
from functools import cache

import pytest

from scripts.seed_data import Dataset, generate
from scripts.seed_inventory import (
    Inventory,
    generate_inventory,
    planted_inventory_facts,
    stock_on_hand,
)

END = date(2026, 10, 27)


@cache
def world() -> tuple[Dataset, Inventory]:
    ds = generate(END)
    return ds, generate_inventory(ds)


def test_deterministic() -> None:
    ds, inv = world()
    assert generate_inventory(ds) == inv


def test_every_dish_has_a_recipe_from_its_own_restaurant() -> None:
    ds, inv = world()
    ing_rest = {i.id: i.restaurant_id for i in inv.ingredients}
    dish_rest = {m.id: m.restaurant_id for m in ds.menu_items}
    assert {r.menu_item_id for r in inv.recipes} == set(dish_rest)
    for r in inv.recipes:
        assert ing_rest[r.ingredient_id] == r.restaurant_id == dish_rest[r.menu_item_id]


def test_movements_are_consistent() -> None:
    _, inv = world()
    for m in inv.movements:
        assert (m.kind == "purchase") == (m.unit_cost is not None)
        if m.kind == "purchase":
            assert m.qty > 0
        if m.kind in ("sale", "waste"):
            assert m.qty < 0
    assert all(q >= 0 for q in stock_on_hand(inv).values())  # never negative


def test_counts_record_what_the_books_expected() -> None:
    _, inv = world()
    assert inv.counts
    for c in inv.counts:
        assert 0 <= c.counted_qty <= c.expected_qty


def test_sales_movements_match_items_sold() -> None:
    ds, inv = world()
    recipe = {(r.menu_item_id, r.ingredient_id): r.qty for r in inv.recipes}
    moz = next(i for i in inv.ingredients if i.restaurant_id == 1 and i.name == "Mussarela")
    expected = sum(
        (recipe.get((oi.menu_item_id, moz.id), Decimal(0)) * oi.qty for oi in ds.order_items),
        Decimal(0),
    )
    sold = -sum(
        (m.qty for m in inv.movements if m.ingredient_id == moz.id and m.kind == "sale"), Decimal(0)
    )
    assert sold == pytest.approx(expected, abs=Decimal("0.1"))


def test_planted_patterns() -> None:
    ds, inv = world()
    f = planted_inventory_facts(ds, inv)
    # 1. Mozzarella runs out before the supplier can deliver; nothing else does.
    assert f["mozzarella"]["cover_days"] < f["mozzarella"]["lead_time_days"]
    assert f["covers_below_lead_time"] == ["Mussarela"]
    # 2. The best seller among pizzas has the lowest margin among main dishes.
    assert f["lowest_margin_main_r1"] == "Pizza margherita"
    # 3. Black beans lose 15% at every count.
    assert f["beans_loss_share"] and all(abs(x - 0.15) < 0.005 for x in f["beans_loss_share"])
    # 4. Salmon +30% in the last 30 days; the temaki margin falls about 10 points.
    assert f["salmon_cost"]["last_30_days"] == pytest.approx(f["salmon_cost"]["before"] * 1.3)
    before, after = f["temaki_margin"]["before"], f["temaki_margin"]["last_30_days"]
    assert 0.52 < before < 0.62 and 0.42 < after < 0.52 and before - after > 0.08


def test_other_ingredients_lose_little() -> None:
    _, inv = world()
    beans = next(i.id for i in inv.ingredients if i.restaurant_id == 2 and i.name == "Feijão preto")
    for c in inv.counts:
        if c.ingredient_id != beans:
            assert 1 - c.counted_qty / c.expected_qty <= Decimal("0.02")
