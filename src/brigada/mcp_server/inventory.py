"""Inventory queries: stock, consumption, dish margins, CMV, waste and cost changes.

Same rules as ``queries.py``: parameterized SQL only, every query scoped by restaurant_id,
joins on (restaurant_id, id) so nothing can cross restaurants. Costs are the weighted mean of
the purchases in the period (falling back to the last purchase before it when there was none).
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

from brigada.mcp_server.db import Conn, local_day_bounds, local_tz
from brigada.mcp_server.errors import InvalidStateError, NotFoundError
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
_OPEN_LO = datetime(1900, 1, 1, tzinfo=UTC)
_OPEN_HI = datetime(9999, 1, 1, tzinfo=UTC)

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

# Recipe cost per dish. ``uncosted`` names the ingredients with no purchase price yet: their
# lines would vanish from the SUM, so callers must refuse instead of understating the cost.
_UNIT_COST_CTE = """
unit_cost AS (
    SELECT r.menu_item_id, sum(r.qty * c.avg_cost) AS cost,
           array_agg(i.name ORDER BY i.name) FILTER (WHERE c.avg_cost IS NULL) AS uncosted
      FROM recipes r
      JOIN cost c ON c.ingredient_id = r.ingredient_id
      JOIN ingredients i ON i.restaurant_id = r.restaurant_id AND i.id = r.ingredient_id
     WHERE r.restaurant_id = %(rid)s GROUP BY r.menu_item_id
)"""


def _f(value: Any, digits: int = 3) -> float:
    return round(float(value or 0), digits)


def _refuse_uncosted(rows: list[dict[str, Any]], end: date) -> None:
    missing = sorted({name for r in rows for name in r["uncosted"] or []})
    if missing:
        dishes = sorted({r["name"] for r in rows if r["uncosted"]})
        raise InvalidStateError(
            f"no purchase cost up to {end} for {', '.join(missing)} "
            f"(used in {', '.join(dishes)}): dish cost cannot be computed for this period"
        )


def _as_of(conn: Conn, restaurant_id: int) -> date:
    """Last day with sales; a restaurant with stock but no sales yet uses its last movement."""
    row = conn.execute(
        "SELECT coalesce(max(d) FILTER (WHERE kind = 'sale'), max(d)) AS d FROM ("
        "  SELECT (at AT TIME ZONE %s)::date AS d, kind FROM stock_movements "
        "  WHERE restaurant_id = %s) m",
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
        # An order late past lead time + safety days stops counting: a delivery that never
        # comes must not hide the shortage forever.
        "       (SELECT coalesce(sum(po.qty) FILTER (WHERE NOT po.late), 0) FROM ("
        "          SELECT qty, ordered_at + make_interval(days => i.lead_time_days "
        "                 + i.safety_days) < now() AS late FROM purchase_orders "
        "           WHERE restaurant_id = i.restaurant_id AND ingredient_id = i.id "
        "             AND status = 'ordered') po) AS on_order, "
        "       (SELECT coalesce(sum(po.qty), 0) FROM purchase_orders po "
        "         WHERE po.restaurant_id = i.restaurant_id AND po.ingredient_id = i.id "
        "           AND po.status = 'ordered' AND po.ordered_at + make_interval(days => "
        "               i.lead_time_days + i.safety_days) < now()) AS overdue_on_order, "
        "       -coalesce(sum(m.qty) FILTER (WHERE m.kind = 'sale' AND m.at >= %(lo)s "
        "                                      AND m.at < %(hi)s), 0) / %(days)s AS daily "
        "FROM ingredients i LEFT JOIN stock_movements m "
        "  ON m.restaurant_id = i.restaurant_id AND m.ingredient_id = i.id "
        "WHERE i.restaurant_id = %(rid)s GROUP BY i.id ORDER BY i.id",
        {"rid": inp.restaurant_id, "lo": lo, "hi": hi, "days": USE_WINDOW_DAYS},
    ).fetchall()
    items = []
    for r in rows:
        daily, on_hand, on_order = float(r["daily"]), float(r["on_hand"]), float(r["on_order"])
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
            on_order=round(on_order, 3),
            overdue_on_order=round(float(r["overdue_on_order"]), 3),
            # An open order already covers the gap: counting it stops the same shortage from
            # being proposed again. Running out before delivery still looks at on_hand only.
            below_reorder_point=daily > 0 and on_hand + on_order < reorder,
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
        f"WITH {_COST_CTE}, {_UNIT_COST_CTE}, "
        "sold AS (SELECT oi.menu_item_id, sum(oi.qty) AS qty FROM order_items oi "
        "  JOIN orders o ON o.id = oi.order_id "
        "  WHERE o.restaurant_id = %(rid)s AND o.created_at >= %(lo)s AND o.created_at < %(hi)s "
        "  GROUP BY oi.menu_item_id) "
        "SELECT m.id, m.name, m.category, m.price, u.cost, u.uncosted, "
        "       u.menu_item_id IS NOT NULL AS has_recipe, coalesce(s.qty, 0) AS qty "
        "FROM menu_items m LEFT JOIN unit_cost u ON u.menu_item_id = m.id "
        "LEFT JOIN sold s ON s.menu_item_id = m.id "
        "WHERE m.restaurant_id = %(rid)s AND (%(cat)s::text IS NULL OR m.category = %(cat)s::text)",
        {"rid": inp.restaurant_id, "lo": lo, "hi": hi, "cat": inp.category},
    ).fetchall()
    _refuse_uncosted(rows, inp.end_date)
    items = []
    for r in (r for r in rows if r["has_recipe"]):
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
        dishes_without_recipe=sorted(r["name"] for r in rows if not r["has_recipe"]),
    )


def cmv(conn: Conn, inp: CmvInput) -> CmvResult:
    ensure_restaurant(conn, inp.restaurant_id)
    lo, hi = local_day_bounds(inp.start_date, inp.end_date)
    rows = conn.execute(
        f"WITH {_COST_CTE}, {_UNIT_COST_CTE} "
        "SELECT m.name, m.category, sum(oi.qty * oi.unit_price) AS revenue, "
        "       sum(oi.qty) * u.cost AS cogs, u.uncosted, u.menu_item_id IS NOT NULL AS has_recipe "
        "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "JOIN menu_items m ON m.id = oi.menu_item_id AND m.restaurant_id = o.restaurant_id "
        "LEFT JOIN unit_cost u ON u.menu_item_id = m.id "
        "WHERE o.restaurant_id = %(rid)s AND o.created_at >= %(lo)s AND o.created_at < %(hi)s "
        "GROUP BY m.id, u.menu_item_id, u.cost, u.uncosted ORDER BY m.category, m.id",
        {"rid": inp.restaurant_id, "lo": lo, "hi": hi},
    ).fetchall()
    _refuse_uncosted(rows, inp.end_date)
    totals: dict[str, list[float]] = {}  # category -> [revenue, covered revenue, cogs]
    for r in rows:
        t = totals.setdefault(r["category"], [0.0, 0.0, 0.0])
        t[0] += float(r["revenue"])
        if r["has_recipe"]:
            t[1] += float(r["revenue"])
            t[2] += float(r["cogs"])
    revenue, covered, cogs = (sum(t[i] for t in totals.values()) for i in range(3))
    groups = (
        [
            CmvGroup(
                key=key,
                revenue=round(rev, 2),
                cost_of_goods=round(cost, 2),
                cmv_pct=round(cost / cov, 4) if cov else None,
            )
            for key, (rev, cov, cost) in totals.items()
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
        cmv_pct=round(cogs / covered, 4) if covered else None,
        revenue_without_recipe=round(revenue - covered, 2),
        dishes_without_recipe=sorted({r["name"] for r in rows if not r["has_recipe"]}),
        groups=groups,
    )


def waste_report(conn: Conn, inp: WasteReportInput) -> WasteReportResult:
    ensure_restaurant(conn, inp.restaurant_id)
    lo = local_day_bounds(inp.start_date, inp.start_date)[0] if inp.start_date else None
    hi = local_day_bounds(inp.end_date, inp.end_date)[1] if inp.end_date else None
    # The cost is the period's mean purchase price (the same rule as cmv and dish_margins),
    # not the mean of the whole history; an open bound means "every purchase on that side".
    params = {
        "rid": inp.restaurant_id,
        "lo": lo or _OPEN_LO,
        "hi": hi or _OPEN_HI,
        "count_lo": lo,
        "count_hi": hi,
    }
    rows = conn.execute(
        f"WITH {_COST_CTE} "
        "SELECT i.id, i.name, i.unit, count(*) AS n, sum(s.expected_qty) AS expected, "
        "       sum(s.counted_qty) AS counted, c.avg_cost "
        "FROM stock_counts s JOIN ingredients i "
        "  ON i.restaurant_id = s.restaurant_id AND i.id = s.ingredient_id "
        "JOIN cost c ON c.ingredient_id = i.id "
        "WHERE s.restaurant_id = %(rid)s "
        "AND (%(count_lo)s::timestamptz IS NULL OR s.at >= %(count_lo)s::timestamptz) "
        "AND (%(count_hi)s::timestamptz IS NULL OR s.at < %(count_hi)s::timestamptz) "
        "GROUP BY i.id, c.avg_cost",
        params,
    ).fetchall()
    items = []
    for r in rows:
        expected, counted = float(r["expected"]), float(r["counted"])
        loss = expected - counted
        cost = r["avg_cost"]
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
                # No purchase price yet: the value is unknown, never zero.
                loss_value=round(loss * float(cost), 2) if cost is not None else None,
            )
        )
    items.sort(key=lambda x: (x.loss_value is None, -(x.loss_value or 0), x.ingredient_id))
    return WasteReportResult(
        restaurant_id=inp.restaurant_id,
        items=items,
        total_loss_value=round(sum(x.loss_value or 0 for x in items), 2),
        uncosted=sorted(x.name for x in items if x.loss_value is None),
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
    if not any(r["pc"] is not None for r in rows):
        raise InvalidStateError(
            f"no purchases between {previous[0]} and {previous[1]} to compare with; "
            "use a smaller recent_days"
        )
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
