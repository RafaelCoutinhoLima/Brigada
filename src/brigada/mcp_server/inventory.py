"""Inventory queries: stock, consumption, dish margins, CMV, waste and cost changes.

Same rules as ``queries.py``: parameterized SQL only, every query scoped by restaurant_id,
joins on (restaurant_id, id) so nothing can cross restaurants. Costs are the weighted mean of
the purchases in the period (falling back to the last purchase before it when there was none).
"""

from datetime import date, timedelta
from typing import Any

from brigada.mcp_server.db import Conn, local_day_bounds, local_tz
from brigada.mcp_server.errors import NotFoundError
from brigada.mcp_server.models import (
    CmvGroup,
    CmvInput,
    CmvResult,
    ConsumptionInput,
    ConsumptionResult,
    CostChange,
    CostChangesInput,
    CostChangesResult,
    DishMargin,
    DishMarginsInput,
    DishMarginsResult,
    IngredientUse,
    StockLevel,
    StockLevelsInput,
    StockLevelsResult,
    WasteLine,
    WasteReportInput,
    WasteReportResult,
)
from brigada.mcp_server.queries import ensure_restaurant

USE_WINDOW_DAYS = 14

# Mean purchase cost per ingredient over [lo, hi), else the last purchase before hi.
_COST_CTE = """
cost AS (
    SELECT i.id AS ingredient_id,
           coalesce(
               (SELECT sum(m.qty * m.unit_cost) / sum(m.qty) FROM stock_movements m
                 WHERE m.restaurant_id = i.restaurant_id AND m.ingredient_id = i.id
                   AND m.kind = 'purchase' AND m.at >= %(lo)s AND m.at < %(hi)s),
               (SELECT m.unit_cost FROM stock_movements m
                 WHERE m.restaurant_id = i.restaurant_id AND m.ingredient_id = i.id
                   AND m.kind = 'purchase' AND m.at < %(hi)s
                 ORDER BY m.at DESC LIMIT 1)
           ) AS avg_cost
      FROM ingredients i WHERE i.restaurant_id = %(rid)s
)"""


def _f(value: Any, digits: int = 3) -> float:
    return round(float(value or 0), digits)


def _as_of(conn: Conn, restaurant_id: int) -> date:
    row = conn.execute(
        "SELECT max((at AT TIME ZONE %s)::date) AS d FROM stock_movements "
        "WHERE restaurant_id = %s AND kind = 'sale'",
        (local_tz().key, restaurant_id),
    ).fetchone()
    if row is None or row["d"] is None:
        raise NotFoundError(f"restaurant {restaurant_id} has no stock movements")
    return row["d"]


def get_ingredient(conn: Conn, restaurant_id: int, ingredient_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, name, unit FROM ingredients WHERE restaurant_id = %s AND id = %s",
        (restaurant_id, ingredient_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"ingredient {ingredient_id} does not exist in restaurant {restaurant_id}"
        )
    return row


def stock_levels(conn: Conn, inp: StockLevelsInput) -> StockLevelsResult:
    ensure_restaurant(conn, inp.restaurant_id)
    as_of = _as_of(conn, inp.restaurant_id)
    lo, hi = local_day_bounds(as_of - timedelta(days=USE_WINDOW_DAYS - 1), as_of)
    rows = conn.execute(
        "SELECT i.id, i.name, i.unit, i.lead_time_days, i.safety_days, "
        "       coalesce(sum(m.qty), 0) AS on_hand, "
        "       -coalesce(sum(m.qty) FILTER (WHERE m.kind = 'sale' AND m.at >= %(lo)s "
        "                                      AND m.at < %(hi)s), 0) / %(days)s AS daily "
        "FROM ingredients i LEFT JOIN stock_movements m "
        "  ON m.restaurant_id = i.restaurant_id AND m.ingredient_id = i.id "
        "WHERE i.restaurant_id = %(rid)s GROUP BY i.id ORDER BY i.id",
        {"rid": inp.restaurant_id, "lo": lo, "hi": hi, "days": USE_WINDOW_DAYS},
    ).fetchall()
    items = []
    for r in rows:
        daily, on_hand = float(r["daily"]), float(r["on_hand"])
        cover = on_hand / daily if daily > 0 else None
        reorder = daily * (r["lead_time_days"] + r["safety_days"])
        level = StockLevel(
            ingredient_id=r["id"],
            name=r["name"],
            unit=r["unit"],
            on_hand=round(on_hand, 3),
            avg_daily_use=round(daily, 3),
            cover_days=round(cover, 2) if cover is not None else None,
            lead_time_days=r["lead_time_days"],
            safety_days=r["safety_days"],
            reorder_point=round(reorder, 3),
            below_reorder_point=daily > 0 and on_hand < reorder,
            runs_out_before_delivery=cover is not None and cover < r["lead_time_days"],
        )
        if level.below_reorder_point or not inp.only_below_reorder_point:
            items.append(level)
    items.sort(key=lambda x: (x.cover_days is None, x.cover_days or 0.0))
    return StockLevelsResult(restaurant_id=inp.restaurant_id, as_of=as_of, items=items)


def ingredient_consumption(conn: Conn, inp: ConsumptionInput) -> ConsumptionResult:
    ensure_restaurant(conn, inp.restaurant_id)
    if inp.ingredient_id is not None:
        get_ingredient(conn, inp.restaurant_id, inp.ingredient_id)
    lo, hi = local_day_bounds(inp.start_date, inp.end_date)
    rows = conn.execute(
        f"WITH {_COST_CTE} "
        "SELECT i.id, i.name, i.unit, c.avg_cost, "
        "  coalesce(sum(m.qty) FILTER (WHERE m.kind = 'purchase'), 0) AS purchased, "
        "  -coalesce(sum(m.qty) FILTER (WHERE m.kind = 'sale'), 0) AS sold, "
        "  -coalesce(sum(m.qty) FILTER (WHERE m.kind = 'waste'), 0) AS waste, "
        "  -coalesce(sum(m.qty) FILTER (WHERE m.kind = 'count_adjust' AND m.qty < 0), 0) AS lost "
        "FROM ingredients i JOIN cost c ON c.ingredient_id = i.id "
        "LEFT JOIN stock_movements m ON m.restaurant_id = i.restaurant_id "
        "  AND m.ingredient_id = i.id AND m.at >= %(lo)s AND m.at < %(hi)s "
        "WHERE i.restaurant_id = %(rid)s AND (%(ing)s::int IS NULL OR i.id = %(ing)s::int) "
        "GROUP BY i.id, c.avg_cost ORDER BY i.id",
        {"rid": inp.restaurant_id, "lo": lo, "hi": hi, "ing": inp.ingredient_id},
    ).fetchall()
    return ConsumptionResult(
        restaurant_id=inp.restaurant_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        items=[
            IngredientUse(
                ingredient_id=r["id"],
                name=r["name"],
                unit=r["unit"],
                purchased=_f(r["purchased"]),
                sold=_f(r["sold"]),
                registered_waste=_f(r["waste"]),
                unexplained_loss=_f(r["lost"]),
                avg_cost=_f(r["avg_cost"], 2) if r["avg_cost"] is not None else None,
            )
            for r in rows
        ],
    )


def dish_margins(conn: Conn, inp: DishMarginsInput) -> DishMarginsResult:
    ensure_restaurant(conn, inp.restaurant_id)
    lo, hi = local_day_bounds(inp.start_date, inp.end_date)
    rows = conn.execute(
        f"WITH {_COST_CTE}, "
        "unit_cost AS (SELECT r.menu_item_id, sum(r.qty * c.avg_cost) AS cost FROM recipes r "
        "  JOIN cost c ON c.ingredient_id = r.ingredient_id "
        "  WHERE r.restaurant_id = %(rid)s GROUP BY r.menu_item_id), "
        "sold AS (SELECT oi.menu_item_id, sum(oi.qty) AS qty FROM order_items oi "
        "  JOIN orders o ON o.id = oi.order_id "
        "  WHERE o.restaurant_id = %(rid)s AND o.created_at >= %(lo)s AND o.created_at < %(hi)s "
        "  GROUP BY oi.menu_item_id) "
        "SELECT m.id, m.name, m.category, m.price, u.cost, coalesce(s.qty, 0) AS qty "
        "FROM menu_items m JOIN unit_cost u ON u.menu_item_id = m.id "
        "LEFT JOIN sold s ON s.menu_item_id = m.id "
        "WHERE m.restaurant_id = %(rid)s AND (%(cat)s::text IS NULL OR m.category = %(cat)s::text)",
        {"rid": inp.restaurant_id, "lo": lo, "hi": hi, "cat": inp.category},
    ).fetchall()
    items = []
    for r in rows:
        price, cost = float(r["price"]), float(r["cost"])
        margin = price - cost
        items.append(
            DishMargin(
                menu_item_id=r["id"],
                name=r["name"],
                category=r["category"],
                price=round(price, 2),
                unit_cost=round(cost, 2),
                margin=round(margin, 2),
                margin_pct=round(margin / price, 4),
                qty_sold=int(r["qty"]),
                contribution=round(margin * int(r["qty"]), 2),
            )
        )
    if inp.order_by == "margin_pct":
        items.sort(key=lambda x: (x.margin_pct, x.menu_item_id))
    else:
        items.sort(key=lambda x: (-x.contribution, x.menu_item_id))
    return DishMarginsResult(
        restaurant_id=inp.restaurant_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        items=items,
    )


def cmv(conn: Conn, inp: CmvInput) -> CmvResult:
    ensure_restaurant(conn, inp.restaurant_id)
    lo, hi = local_day_bounds(inp.start_date, inp.end_date)
    rows = conn.execute(
        f"WITH {_COST_CTE}, "
        "unit_cost AS (SELECT r.menu_item_id, sum(r.qty * c.avg_cost) AS cost FROM recipes r "
        "  JOIN cost c ON c.ingredient_id = r.ingredient_id "
        "  WHERE r.restaurant_id = %(rid)s GROUP BY r.menu_item_id) "
        "SELECT m.category, sum(oi.qty * oi.unit_price) AS revenue, "
        "       sum(oi.qty * u.cost) AS cogs "
        "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "JOIN menu_items m ON m.id = oi.menu_item_id AND m.restaurant_id = o.restaurant_id "
        "JOIN unit_cost u ON u.menu_item_id = m.id "
        "WHERE o.restaurant_id = %(rid)s AND o.created_at >= %(lo)s AND o.created_at < %(hi)s "
        "GROUP BY m.category ORDER BY m.category",
        {"rid": inp.restaurant_id, "lo": lo, "hi": hi},
    ).fetchall()
    revenue = sum(float(r["revenue"]) for r in rows)
    cogs = sum(float(r["cogs"]) for r in rows)
    groups = (
        [
            CmvGroup(
                key=r["category"],
                revenue=round(float(r["revenue"]), 2),
                cost_of_goods=round(float(r["cogs"]), 2),
                cmv_pct=round(float(r["cogs"]) / float(r["revenue"]), 4) if r["revenue"] else None,
            )
            for r in rows
        ]
        if inp.group_by == "category"
        else []
    )
    return CmvResult(
        restaurant_id=inp.restaurant_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        revenue=round(revenue, 2),
        cost_of_goods=round(cogs, 2),
        cmv_pct=round(cogs / revenue, 4) if revenue else None,
        groups=groups,
    )


def waste_report(conn: Conn, inp: WasteReportInput) -> WasteReportResult:
    ensure_restaurant(conn, inp.restaurant_id)
    lo = local_day_bounds(inp.start_date, inp.start_date)[0] if inp.start_date else None
    hi = local_day_bounds(inp.end_date, inp.end_date)[1] if inp.end_date else None
    params = {"rid": inp.restaurant_id, "lo": lo, "hi": hi}
    rows = conn.execute(
        "WITH cost AS (SELECT i.id AS ingredient_id, "
        "  (SELECT sum(m.qty * m.unit_cost) / sum(m.qty) FROM stock_movements m "
        "    WHERE m.restaurant_id = i.restaurant_id AND m.ingredient_id = i.id "
        "      AND m.kind = 'purchase' "
        "      AND (%(hi)s::timestamptz IS NULL OR m.at < %(hi)s::timestamptz)) AS avg_cost "
        "  FROM ingredients i WHERE i.restaurant_id = %(rid)s) "
        "SELECT i.id, i.name, i.unit, count(*) AS n, sum(s.expected_qty) AS expected, "
        "       sum(s.counted_qty) AS counted, c.avg_cost "
        "FROM stock_counts s JOIN ingredients i "
        "  ON i.restaurant_id = s.restaurant_id AND i.id = s.ingredient_id "
        "JOIN cost c ON c.ingredient_id = i.id "
        "WHERE s.restaurant_id = %(rid)s "
        "AND (%(lo)s::timestamptz IS NULL OR s.at >= %(lo)s::timestamptz) "
        "AND (%(hi)s::timestamptz IS NULL OR s.at < %(hi)s::timestamptz) "
        "GROUP BY i.id, c.avg_cost",
        params,
    ).fetchall()
    items = []
    for r in rows:
        expected, counted = float(r["expected"]), float(r["counted"])
        loss = expected - counted
        items.append(
            WasteLine(
                ingredient_id=r["id"],
                name=r["name"],
                unit=r["unit"],
                counts=r["n"],
                expected=round(expected, 3),
                counted=round(counted, 3),
                loss=round(loss, 3),
                loss_share=round(loss / expected, 4) if expected else 0.0,
                loss_value=round(loss * float(r["avg_cost"] or 0), 2),
            )
        )
    items.sort(key=lambda x: (-x.loss_value, x.ingredient_id))
    return WasteReportResult(
        restaurant_id=inp.restaurant_id,
        items=items,
        total_loss_value=round(sum(x.loss_value for x in items), 2),
    )


def cost_changes(conn: Conn, inp: CostChangesInput) -> CostChangesResult:
    ensure_restaurant(conn, inp.restaurant_id)
    as_of = _as_of(conn, inp.restaurant_id)
    recent = (as_of - timedelta(days=inp.recent_days - 1), as_of)
    previous = (recent[0] - timedelta(days=inp.recent_days), recent[0] - timedelta(days=1))
    r_lo, r_hi = local_day_bounds(*recent)
    p_lo, p_hi = local_day_bounds(*previous)
    rows = conn.execute(
        "SELECT i.id, i.name, i.unit, "
        "  sum(m.qty * m.unit_cost) FILTER (WHERE m.at >= %(r_lo)s AND m.at < %(r_hi)s) "
        "    / nullif(sum(m.qty) FILTER (WHERE m.at >= %(r_lo)s AND m.at < %(r_hi)s), 0) AS rc, "
        "  sum(m.qty * m.unit_cost) FILTER (WHERE m.at >= %(p_lo)s AND m.at < %(p_hi)s) "
        "    / nullif(sum(m.qty) FILTER (WHERE m.at >= %(p_lo)s AND m.at < %(p_hi)s), 0) AS pc "
        "FROM ingredients i JOIN stock_movements m "
        "  ON m.restaurant_id = i.restaurant_id AND m.ingredient_id = i.id AND m.kind = 'purchase' "
        "WHERE i.restaurant_id = %(rid)s GROUP BY i.id",
        {"rid": inp.restaurant_id, "r_lo": r_lo, "r_hi": r_hi, "p_lo": p_lo, "p_hi": p_hi},
    ).fetchall()
    items = [
        CostChange(
            ingredient_id=r["id"],
            name=r["name"],
            unit=r["unit"],
            previous_cost=_f(r["pc"], 2),
            recent_cost=_f(r["rc"], 2),
            change_pct=round(float(r["rc"]) / float(r["pc"]) - 1, 4),
        )
        for r in rows
        if r["rc"] is not None and r["pc"] is not None
    ]
    items.sort(key=lambda x: (-x.change_pct, x.ingredient_id))
    return CostChangesResult(
        restaurant_id=inp.restaurant_id, recent=recent, previous=previous, items=items
    )
