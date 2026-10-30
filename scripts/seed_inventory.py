"""Synthetic inventory built on top of the sales dataset (``scripts.seed_data``).

Ingredients, recipes (fichas técnicas), stock movements and physical counts, all derived from
the orders that already exist, so the sales world and its eval ground truth do not change.
Deterministic for a given ``Dataset``.

Planted patterns (the inventory eval ground truth; see ``planted_inventory_facts``):

1. Restaurant 1: mozzarella ends the period with about 2 days of cover while the supplier
   takes 3 days, so it runs out before the next delivery.
2. Restaurant 1: Pizza margherita sells a lot but has the lowest margin among main dishes.
3. Restaurant 2: black beans lose 15% between the books and every physical count.
4. Restaurant 3: salmon costs 30% more in the last 30 days, so the temaki margin falls from
   about 55% to about 45%.
"""

import random
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from scripts.seed_data import TZ, Dataset, period_start

QTY = Decimal("0.001")
CENTS = Decimal("0.01")

MOZZARELLA = "Mussarela"
MOZZARELLA_COVER_DAYS = Decimal("2")
BEANS = "Feijão preto"
BEANS_LOSS = Decimal("0.15")
SALMON = "Salmão"
SALMON_RISE = Decimal("1.30")
SALMON_RISE_DAYS = 30
COUNT_EVERY_DAYS = 30
NORMAL_LOSS_MAX = Decimal("0.02")


@dataclass(frozen=True, slots=True)
class Ingredient:
    id: int
    restaurant_id: int
    name: str
    unit: str
    lead_time_days: int
    safety_days: int
    base_cost: Decimal  # per unit, before any planted change


@dataclass(frozen=True, slots=True)
class Recipe:
    restaurant_id: int
    menu_item_id: int
    ingredient_id: int
    qty: Decimal


@dataclass(frozen=True, slots=True)
class Movement:
    id: int
    restaurant_id: int
    ingredient_id: int
    kind: str
    qty: Decimal
    unit_cost: Decimal | None
    at: datetime


@dataclass(frozen=True, slots=True)
class StockCount:
    id: int
    restaurant_id: int
    ingredient_id: int
    counted_qty: Decimal
    expected_qty: Decimal
    at: datetime


@dataclass(frozen=True, slots=True)
class Inventory:
    ingredients: list[Ingredient]
    recipes: list[Recipe]
    movements: list[Movement]
    counts: list[StockCount]


# (restaurant_id, name, unit, cost per unit, lead time days, safety days)
_INGREDIENTS: tuple[tuple[int, str, str, str, int, int], ...] = (
    (1, "Farinha de trigo", "kg", "6.00", 2, 2),
    (1, "Tomate", "kg", "8.00", 1, 1),
    (1, MOZZARELLA, "kg", "42.00", 3, 1),
    (1, "Burrata", "un", "18.00", 2, 1),
    (1, "Filé mignon", "kg", "75.00", 2, 1),
    (1, "Carne moída", "kg", "38.00", 2, 1),
    (1, "Massa seca", "kg", "14.00", 3, 2),
    (1, "Parmesão", "kg", "90.00", 4, 2),
    (1, "Creme de leite", "l", "22.00", 2, 1),
    (1, "Funghi", "kg", "120.00", 4, 2),
    (1, "Arroz arbóreo", "kg", "32.00", 4, 2),
    (1, "Batata", "kg", "6.00", 1, 1),
    (1, "Ovo", "un", "0.90", 2, 1),
    (1, "Mascarpone", "kg", "60.00", 3, 1),
    (1, "Leite", "l", "5.00", 1, 1),
    (1, "Água mineral", "un", "1.50", 3, 2),
    (1, "Refrigerante lata", "un", "3.00", 3, 2),
    (1, "Laranja", "kg", "5.00", 1, 1),
    (1, "Vinho tinto", "l", "60.00", 5, 3),
    (2, BEANS, "kg", "9.00", 2, 1),
    (2, "Carne seca", "kg", "48.00", 3, 1),
    (2, "Linguiça", "kg", "30.00", 2, 1),
    (2, "Arroz", "kg", "6.00", 3, 2),
    (2, "Couve", "kg", "10.00", 1, 1),
    (2, "Farinha de mandioca", "kg", "8.00", 3, 2),
    (2, "Frango", "kg", "18.00", 2, 1),
    (2, "Quiabo", "kg", "12.00", 1, 1),
    (2, "Picanha", "kg", "85.00", 2, 1),
    (2, "Peixe", "kg", "45.00", 1, 1),
    (2, "Leite de coco", "l", "18.00", 3, 2),
    (2, "Carne bovina", "kg", "40.00", 2, 1),
    (2, "Cebola", "kg", "5.00", 2, 1),
    (2, "Queijo minas", "kg", "38.00", 2, 1),
    (2, "Mandioca", "kg", "5.00", 1, 1),
    (2, "Massa de pastel", "kg", "14.00", 2, 1),
    (2, "Barriga suína", "kg", "25.00", 2, 1),
    (2, "Leite condensado", "un", "7.00", 3, 2),
    (2, "Doce de leite", "kg", "20.00", 3, 2),
    (2, "Maracujá", "kg", "9.00", 1, 1),
    (2, "Guaraná lata", "un", "2.50", 3, 2),
    (2, "Cerveja long neck", "un", "4.50", 3, 2),
    (3, SALMON, "kg", "110.00", 1, 1),
    (3, "Arroz japonês", "kg", "8.00", 3, 2),
    (3, "Alga nori", "un", "0.80", 5, 3),
    (3, "Cream cheese", "kg", "40.00", 3, 1),
    (3, "Massa de gyoza", "kg", "20.00", 3, 1),
    (3, "Carne suína", "kg", "28.00", 2, 1),
    (3, "Pepino", "kg", "6.00", 1, 1),
    (3, "Missô", "kg", "35.00", 5, 3),
    (3, "Tofu", "kg", "24.00", 2, 1),
    (3, "Edamame", "kg", "30.00", 3, 2),
    (3, "Macarrão yakisoba", "kg", "16.00", 3, 2),
    (3, "Legumes", "kg", "9.00", 1, 1),
    (3, "Banana", "kg", "5.00", 1, 1),
    (3, "Mochi", "un", "4.00", 5, 3),
    (3, "Chá verde", "un", "1.20", 5, 3),
    (3, "Refrigerante lata", "un", "3.00", 3, 2),
    (3, "Saquê", "l", "70.00", 5, 3),
)

# dish name -> [(ingredient name, qty per dish)]
_RECIPES: dict[int, dict[str, list[tuple[str, str]]]] = {
    1: {
        "Bruschetta de tomate": [("Farinha de trigo", "0.080"), ("Tomate", "0.100")],
        "Burrata com pesto": [("Burrata", "1"), ("Tomate", "0.080")],
        "Carpaccio": [("Filé mignon", "0.120"), ("Parmesão", "0.020")],
        "Lasanha à bolonhesa": [
            ("Massa seca", "0.150"),
            ("Carne moída", "0.150"),
            (MOZZARELLA, "0.080"),
            ("Tomate", "0.100"),
        ],
        "Spaghetti carbonara": [("Massa seca", "0.150"), ("Ovo", "2"), ("Parmesão", "0.040")],
        "Risoto de funghi": [
            ("Arroz arbóreo", "0.100"),
            ("Funghi", "0.060"),
            ("Parmesão", "0.030"),
            ("Creme de leite", "0.050"),
        ],
        "Nhoque ao sugo": [("Batata", "0.250"), ("Farinha de trigo", "0.050"), ("Tomate", "0.120")],
        # The planted low margin: a generous pizza on an expensive cheese, at a mid price.
        "Pizza margherita": [
            ("Farinha de trigo", "0.250"),
            (MOZZARELLA, "0.550"),
            ("Tomate", "0.150"),
        ],
        "Fettuccine alfredo": [
            ("Massa seca", "0.150"),
            ("Creme de leite", "0.120"),
            ("Parmesão", "0.040"),
        ],
        "Tiramisù": [("Mascarpone", "0.080"), ("Ovo", "1")],
        "Panna cotta": [("Creme de leite", "0.120"), ("Leite", "0.050")],
        "Cannoli": [("Farinha de trigo", "0.060"), ("Mascarpone", "0.040")],
        "Água mineral": [("Água mineral", "1")],
        "Refrigerante lata": [("Refrigerante lata", "1")],
        "Suco de laranja": [("Laranja", "0.400")],
        "Vinho tinto taça": [("Vinho tinto", "0.150")],
    },
    2: {
        "Pastel de queijo": [("Massa de pastel", "0.080"), ("Queijo minas", "0.060")],
        "Bolinho de mandioca": [("Mandioca", "0.200"), ("Queijo minas", "0.040")],
        "Torresmo": [("Barriga suína", "0.250")],
        "Feijoada da casa": [
            (BEANS, "0.180"),
            ("Carne seca", "0.100"),
            ("Linguiça", "0.080"),
            ("Arroz", "0.100"),
            ("Couve", "0.050"),
            ("Farinha de mandioca", "0.040"),
        ],
        "Frango com quiabo": [("Frango", "0.300"), ("Quiabo", "0.120"), ("Arroz", "0.100")],
        "Tutu à mineira": [
            (BEANS, "0.150"),
            ("Linguiça", "0.080"),
            ("Farinha de mandioca", "0.060"),
            ("Couve", "0.050"),
        ],
        "Picanha na chapa": [("Picanha", "0.300"), ("Arroz", "0.100")],
        "Moqueca de peixe": [("Peixe", "0.300"), ("Leite de coco", "0.150"), ("Arroz", "0.100")],
        "Bife acebolado": [("Carne bovina", "0.200"), ("Cebola", "0.100"), ("Arroz", "0.100")],
        "Pudim de leite": [("Leite condensado", "0.5")],
        "Doce de leite com queijo": [("Doce de leite", "0.080"), ("Queijo minas", "0.050")],
        "Suco de maracujá": [("Maracujá", "0.250")],
        "Guaraná lata": [("Guaraná lata", "1")],
        "Cerveja long neck": [("Cerveja long neck", "1")],
    },
    3: {
        "Gyoza": [("Massa de gyoza", "0.060"), ("Carne suína", "0.080")],
        "Sunomono": [("Pepino", "0.150")],
        "Missoshiru": [("Missô", "0.030"), ("Tofu", "0.050")],
        "Edamame": [("Edamame", "0.150")],
        "Combinado 20 peças": [
            (SALMON, "0.200"),
            ("Arroz japonês", "0.200"),
            ("Alga nori", "2"),
            ("Cream cheese", "0.040"),
        ],
        "Combinado 40 peças": [
            (SALMON, "0.380"),
            ("Arroz japonês", "0.380"),
            ("Alga nori", "4"),
            ("Cream cheese", "0.080"),
        ],
        # The planted margin squeeze: salmon is most of its cost.
        "Temaki de salmão": [
            (SALMON, "0.120"),
            ("Arroz japonês", "0.080"),
            ("Alga nori", "1"),
            ("Cream cheese", "0.050"),
        ],
        "Uramaki filadélfia": [
            (SALMON, "0.080"),
            ("Arroz japonês", "0.120"),
            ("Cream cheese", "0.040"),
            ("Alga nori", "1"),
        ],
        "Yakisoba": [
            ("Macarrão yakisoba", "0.150"),
            ("Legumes", "0.200"),
            ("Carne suína", "0.080"),
        ],
        "Sashimi de salmão": [(SALMON, "0.200")],
        "Hot roll": [(SALMON, "0.070"), ("Arroz japonês", "0.120"), ("Cream cheese", "0.040")],
        "Harumaki de banana": [("Banana", "0.150"), ("Massa de gyoza", "0.040")],
        "Mochi": [("Mochi", "2")],
        "Chá verde": [("Chá verde", "1")],
        "Refrigerante lata": [("Refrigerante lata", "1")],
        "Saquê dose": [("Saquê", "0.050")],
    },
}


def _q(value: Decimal) -> Decimal:
    return value.quantize(QTY, rounding=ROUND_HALF_UP)


def _cost_on(ing: Ingredient, day: date, end_date: date) -> Decimal:
    if ing.name == SALMON and ing.restaurant_id == 3:
        if day > end_date - timedelta(days=SALMON_RISE_DAYS):
            return (ing.base_cost * SALMON_RISE).quantize(CENTS, rounding=ROUND_HALF_UP)
    return ing.base_cost


def _ts(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=TZ)


def generate_inventory(ds: Dataset, seed: int = 42) -> Inventory:
    rng = random.Random(f"inventory-{seed}")
    ingredients = [
        Ingredient(i, rid, name, unit, lead, safety, Decimal(cost))
        for i, (rid, name, unit, cost, lead, safety) in enumerate(_INGREDIENTS, start=1)
    ]
    by_name = {(i.restaurant_id, i.name): i for i in ingredients}
    items = {(m.restaurant_id, m.name): m for m in ds.menu_items}
    recipes: list[Recipe] = []
    for rid, dishes in _RECIPES.items():
        for dish, parts in dishes.items():
            item = items[(rid, dish)]
            for name, qty in parts:
                recipes.append(Recipe(rid, item.id, by_name[(rid, name)].id, Decimal(qty)))
    missing = {(m.restaurant_id, m.name) for m in ds.menu_items} - {
        (rid, d) for rid, dishes in _RECIPES.items() for d in dishes
    }
    if missing:
        raise ValueError(f"dishes without a recipe: {sorted(missing)}")

    # Daily consumption per ingredient, from the items actually sold.
    recipe_of: dict[int, list[Recipe]] = defaultdict(list)
    for r in recipes:
        recipe_of[r.menu_item_id].append(r)
    order_day = {o.id: o.created_at.astimezone(TZ).date() for o in ds.orders}
    daily: dict[tuple[int, date], Decimal] = defaultdict(Decimal)
    for oi in ds.order_items:
        for r in recipe_of[oi.menu_item_id]:
            daily[(r.ingredient_id, order_day[oi.order_id])] += r.qty * oi.qty

    start, end = period_start(ds.end_date), ds.end_date
    days = [start + timedelta(days=d) for d in range((end - start).days + 1)]
    movements: list[Movement] = []
    counts: list[StockCount] = []
    next_id = iter(range(1, 10**9))

    for ing in ingredients:
        balance = Decimal(0)
        use = [daily.get((ing.id, d), Decimal(0)) for d in days]
        avg = sum(use, Decimal(0)) / len(days)
        loss_rate = BEANS_LOSS if (ing.restaurant_id, ing.name) == (2, BEANS) else None
        purchase_days = [d for i, d in enumerate(days) if i % 7 == 0]
        for i, day in enumerate(days):
            if day in purchase_days:
                # Buy what the next week needs plus the safety stock, minus what is on hand.
                horizon = sum(use[i : i + 7], Decimal(0))
                target = horizon + avg * (ing.safety_days + ing.lead_time_days)
                qty = _q(max(target - balance, Decimal(0)) * Decimal("1.10"))
                if qty > 0:
                    movements.append(
                        Movement(
                            next(next_id),
                            ing.restaurant_id,
                            ing.id,
                            "purchase",
                            qty,
                            _cost_on(ing, day, end),
                            _ts(day, 8),
                        )
                    )
                    balance += qty
            if use[i] > 0:
                movements.append(
                    Movement(
                        next(next_id),
                        ing.restaurant_id,
                        ing.id,
                        "sale",
                        -_q(use[i]),
                        None,
                        _ts(day, 23),
                    )
                )
                balance -= _q(use[i])
            if i > 0 and i % COUNT_EVERY_DAYS == 0 and balance > 0:
                rate = (
                    loss_rate
                    if loss_rate is not None
                    else Decimal(str(round(rng.uniform(0, float(NORMAL_LOSS_MAX)), 4)))
                )
                counted = _q(balance * (1 - rate))
                count = StockCount(
                    next(next_id), ing.restaurant_id, ing.id, counted, _q(balance), _ts(day, 23)
                )
                counts.append(count)
                if counted != balance:
                    movements.append(
                        Movement(
                            next(next_id),
                            ing.restaurant_id,
                            ing.id,
                            "count_adjust",
                            counted - balance,
                            None,
                            count.at + timedelta(minutes=1),
                        )
                    )
                balance = counted

        # Final cover: mozzarella is planted short; everything else ends comfortably stocked.
        recent = sum(use[-14:], Decimal(0)) / 14
        if recent > 0:
            planted = (ing.restaurant_id, ing.name) == (1, MOZZARELLA)
            wanted = recent * (
                MOZZARELLA_COVER_DAYS
                if planted
                else Decimal(ing.lead_time_days + ing.safety_days + 4)
            )
            delta = _q(wanted - balance)
            if planted or delta > 0:
                if delta > 0:
                    movements.append(
                        Movement(
                            next(next_id),
                            ing.restaurant_id,
                            ing.id,
                            "purchase",
                            delta,
                            _cost_on(ing, end, end),
                            _ts(end, 9),
                        )
                    )
                elif delta < 0:
                    # Plant the shortage by buying less the last time, not with a fake loss.
                    last = max(
                        (
                            k
                            for k, m in enumerate(movements)
                            if m.ingredient_id == ing.id and m.kind == "purchase"
                        ),
                    )
                    later_counts = [
                        c for c in counts if c.ingredient_id == ing.id and c.at > movements[last].at
                    ]
                    if later_counts or movements[last].qty <= -delta:
                        raise ValueError(f"cannot plant the {ing.name} shortage by purchase")
                    movements[last] = replace(movements[last], qty=movements[last].qty + delta)
    return Inventory(ingredients, recipes, movements, counts)


# --------------------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------------------


def stock_on_hand(inv: Inventory) -> dict[int, Decimal]:
    out: dict[int, Decimal] = defaultdict(Decimal)
    for m in inv.movements:
        out[m.ingredient_id] += m.qty
    return dict(out)


def average_cost(inv: Inventory, ingredient_id: int, start: date, end: date) -> Decimal:
    """Mean purchase cost in start..end, else the last purchase before end (as the tools do)."""
    buys = [m for m in inv.movements if m.ingredient_id == ingredient_id and m.kind == "purchase"]
    window = [m for m in buys if start <= m.at.astimezone(TZ).date() <= end]
    qty = sum((m.qty for m in window), Decimal(0))
    if qty:
        return sum((m.qty * (m.unit_cost or 0) for m in window), Decimal(0)) / qty
    before = [m for m in buys if m.at.astimezone(TZ).date() <= end]
    if not before:
        raise ValueError(f"no purchases of ingredient {ingredient_id} up to {end}")
    return max(before, key=lambda m: m.at).unit_cost or Decimal(0)


def dish_cost(inv: Inventory, menu_item_id: int, start: date, end: date) -> Decimal:
    return sum(
        (
            r.qty * average_cost(inv, r.ingredient_id, start, end)
            for r in inv.recipes
            if r.menu_item_id == menu_item_id
        ),
        Decimal(0),
    )


def _named(ds: Dataset, rid: int, name: str) -> int:
    return next(m.id for m in ds.menu_items if m.restaurant_id == rid and m.name == name)


def planted_inventory_facts(ds: Dataset, inv: Inventory) -> dict[str, Any]:
    end = ds.end_date
    start = period_start(end)
    last30 = end - timedelta(days=SALMON_RISE_DAYS - 1)
    before = (start, last30 - timedelta(days=1))
    ing = {(i.restaurant_id, i.name): i for i in inv.ingredients}
    on_hand = stock_on_hand(inv)

    def cover(i: Ingredient) -> Decimal:
        sold = [
            m
            for m in inv.movements
            if m.ingredient_id == i.id
            and m.kind == "sale"
            and m.at.astimezone(TZ).date() > end - timedelta(days=14)
        ]
        daily = -sum((m.qty for m in sold), Decimal(0)) / 14
        return on_hand[i.id] / daily

    moz = ing[(1, MOZZARELLA)]
    beans = ing[(2, BEANS)]
    beans_counts = [c for c in inv.counts if c.ingredient_id == beans.id]
    price = {m.id: m.price for m in ds.menu_items}

    def margin(item_id: int, s: date, e: date) -> Decimal:
        return (price[item_id] - dish_cost(inv, item_id, s, e)) / price[item_id]

    mains_1 = [m for m in ds.menu_items if m.restaurant_id == 1 and m.category == "principal"]
    temaki = _named(ds, 3, "Temaki de salmão")
    salmon = ing[(3, SALMON)]
    return {
        "mozzarella": {
            "cover_days": round(float(cover(moz)), 2),
            "lead_time_days": moz.lead_time_days,
        },
        "lowest_margin_main_r1": min(mains_1, key=lambda m: margin(m.id, start, end)).name,
        "beans_loss_share": [
            round(float(1 - c.counted_qty / c.expected_qty), 4) for c in beans_counts
        ],
        "salmon_cost": {
            "before": float(average_cost(inv, salmon.id, *before)),
            "last_30_days": float(average_cost(inv, salmon.id, last30, end)),
        },
        "temaki_margin": {
            "before": round(float(margin(temaki, *before)), 4),
            "last_30_days": round(float(margin(temaki, last30, end)), 4),
        },
        "covers_below_lead_time": sorted(
            i.name
            for i in inv.ingredients
            if on_hand.get(i.id) is not None
            and _has_sales(inv, i.id)
            and cover(i) < i.lead_time_days
        ),
    }


def _has_sales(inv: Inventory, ingredient_id: int) -> bool:
    return any(m.ingredient_id == ingredient_id and m.kind == "sale" for m in inv.movements)


def rows(inv: Inventory) -> dict[str, tuple[tuple[str, ...], Iterable[tuple[Any, ...]]]]:
    return {
        "ingredients": (
            ("id", "restaurant_id", "name", "unit", "lead_time_days", "safety_days"),
            [
                (i.id, i.restaurant_id, i.name, i.unit, i.lead_time_days, i.safety_days)
                for i in inv.ingredients
            ],
        ),
        "recipes": (
            ("restaurant_id", "menu_item_id", "ingredient_id", "qty"),
            [(r.restaurant_id, r.menu_item_id, r.ingredient_id, r.qty) for r in inv.recipes],
        ),
        "stock_movements": (
            ("id", "restaurant_id", "ingredient_id", "kind", "qty", "unit_cost", "at"),
            [
                (m.id, m.restaurant_id, m.ingredient_id, m.kind, m.qty, m.unit_cost, m.at)
                for m in inv.movements
            ],
        ),
        "stock_counts": (
            ("id", "restaurant_id", "ingredient_id", "counted_qty", "expected_qty", "at"),
            [
                (c.id, c.restaurant_id, c.ingredient_id, c.counted_qty, c.expected_qty, c.at)
                for c in inv.counts
            ],
        ),
    }
