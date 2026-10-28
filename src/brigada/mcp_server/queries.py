"""Pure read queries behind the MCP read tools.

Each function takes an open connection and a validated input model and returns an output
model. No MCP, no audit, no connection management: that lives in ``server.py``. All SQL is
static text with bound parameters; the only variable part is a fragment picked from a
fixed whitelist keyed by an already-validated ``Literal`` (``group_by``).
"""

from datetime import datetime
from typing import Any

from psycopg import sql

from brigada.mcp_server.db import Conn, local_day_bounds, local_tz
from brigada.mcp_server.errors import NotFoundError
from brigada.mcp_server.models import (
    WEEKDAY_NAMES,
    DeliveryGroup,
    DeliveryTimesInput,
    DeliveryTimesResult,
    ListMenuItemsInput,
    ListMenuItemsResult,
    MenuItem,
    Review,
    ReviewsForItemInput,
    ReviewsForItemResult,
    SalesByPeriodInput,
    SalesByPeriodResult,
    SalesGroup,
    TopItem,
    TopItemsInput,
    TopItemsResult,
)

CHANNELS: tuple[str, ...] = ("app", "telefone", "balcao")


def _money(value: Any) -> float:
    return round(float(value), 2)


def ensure_restaurant(conn: Conn, restaurant_id: int) -> None:
    row = conn.execute("SELECT 1 FROM restaurants WHERE id = %s", (restaurant_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"restaurant {restaurant_id} does not exist")


def get_menu_item(conn: Conn, restaurant_id: int, menu_item_id: int) -> MenuItem:
    """Return the item, enforcing that it belongs to ``restaurant_id`` (tenant guard)."""
    row = conn.execute(
        "SELECT id, name, category, price, active FROM menu_items "
        "WHERE id = %s AND restaurant_id = %s",
        (menu_item_id, restaurant_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"menu item {menu_item_id} does not exist in restaurant {restaurant_id}"
        )
    return MenuItem(
        menu_item_id=row["id"],
        name=row["name"],
        category=row["category"],
        price=_money(row["price"]),
        active=row["active"],
    )


# --------------------------------------------------------------------------------------
# sales_by_period
# --------------------------------------------------------------------------------------

# Each "keys" CTE enumerates every bucket so the result is zero-filled: a day or channel
# with no orders shows up as 0, which is itself a signal (e.g. an app outage).
_SALES_KEYS: dict[str, str] = {
    "day": "SELECT d::date AS k, to_char(d::date, 'YYYY-MM-DD') AS label "
    "FROM generate_series(%(start)s::date, %(end)s::date, interval '1 day') AS d",
    "channel": "SELECT c AS k, c AS label, ord FROM unnest(%(channels)s::text[]) "
    "WITH ORDINALITY AS t(c, ord)",
    "hour": "SELECT h AS k, h::text AS label FROM generate_series(0, 23) AS h",
    "weekday": "SELECT w AS k, (%(weekday_names)s::text[])[w] AS label "
    "FROM generate_series(1, 7) AS w",
}
_SALES_BUCKET: dict[str, str] = {
    "day": "(created_at AT TIME ZONE %(tz)s)::date",
    "channel": "channel",
    "hour": "extract(hour FROM created_at AT TIME ZONE %(tz)s)::int",
    "weekday": "extract(isodow FROM created_at AT TIME ZONE %(tz)s)::int",
}


def sales_by_period(conn: Conn, inp: SalesByPeriodInput) -> SalesByPeriodResult:
    ensure_restaurant(conn, inp.restaurant_id)
    lo, hi = local_day_bounds(inp.start_date, inp.end_date)
    params: dict[str, Any] = {
        "rid": inp.restaurant_id,
        "lo": lo,
        "hi": hi,
        "tz": local_tz().key,
        "start": inp.start_date,
        "end": inp.end_date,
        "channels": list(CHANNELS),
        "weekday_names": [WEEKDAY_NAMES[i] for i in range(1, 8)],
    }
    totals = conn.execute(
        "SELECT count(*) AS orders, coalesce(sum(total), 0) AS revenue FROM orders "
        "WHERE restaurant_id = %(rid)s AND created_at >= %(lo)s AND created_at < %(hi)s",
        params,
    ).fetchone()
    assert totals is not None  # aggregate without GROUP BY always returns one row

    groups: list[SalesGroup] = []
    if inp.group_by != "none":
        order = "keys.ord" if inp.group_by == "channel" else "keys.k"
        query = sql.SQL(
            "WITH keys AS ({keys}), "
            "o AS (SELECT {bucket} AS k, total FROM orders "
            "      WHERE restaurant_id = %(rid)s AND created_at >= %(lo)s "
            "        AND created_at < %(hi)s) "
            "SELECT keys.label, count(o.total) AS orders, coalesce(sum(o.total), 0) AS revenue "
            "FROM keys LEFT JOIN o ON o.k = keys.k "
            "GROUP BY keys.k, keys.label{extra_group} ORDER BY {order}"
        ).format(
            keys=sql.SQL(_SALES_KEYS[inp.group_by]),
            bucket=sql.SQL(_SALES_BUCKET[inp.group_by]),
            extra_group=sql.SQL(", keys.ord" if inp.group_by == "channel" else ""),
            order=sql.SQL(order),
        )
        groups = [
            SalesGroup(key=r["label"], orders=r["orders"], revenue=_money(r["revenue"]))
            for r in conn.execute(query, params).fetchall()
        ]

    n_orders: int = totals["orders"]
    revenue = _money(totals["revenue"])
    return SalesByPeriodResult(
        restaurant_id=inp.restaurant_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        group_by=inp.group_by,
        total_orders=n_orders,
        total_revenue=revenue,
        average_ticket=round(revenue / n_orders, 2) if n_orders else None,
        groups=groups,
    )


# --------------------------------------------------------------------------------------
# top_items
# --------------------------------------------------------------------------------------

_TOP_ORDER: dict[str, str] = {
    "revenue": "revenue DESC, qty DESC, m.id",
    "qty": "qty DESC, revenue DESC, m.id",
}


def top_items(conn: Conn, inp: TopItemsInput) -> TopItemsResult:
    ensure_restaurant(conn, inp.restaurant_id)
    lo, hi = local_day_bounds(inp.start_date, inp.end_date)
    query = sql.SQL(
        "SELECT m.id, m.name, m.category, sum(oi.qty) AS qty, "
        "       sum(oi.qty * oi.unit_price) AS revenue, count(DISTINCT o.id) AS orders "
        "FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "JOIN menu_items m ON m.id = oi.menu_item_id "
        "WHERE o.restaurant_id = %(rid)s AND o.created_at >= %(lo)s AND o.created_at < %(hi)s "
        "GROUP BY m.id, m.name, m.category "
        "ORDER BY {order} LIMIT %(limit)s"
    ).format(order=sql.SQL(_TOP_ORDER[inp.order_by]))
    rows = conn.execute(
        query, {"rid": inp.restaurant_id, "lo": lo, "hi": hi, "limit": inp.limit}
    ).fetchall()
    return TopItemsResult(
        restaurant_id=inp.restaurant_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        order_by=inp.order_by,
        items=[
            TopItem(
                menu_item_id=r["id"],
                name=r["name"],
                category=r["category"],
                qty=int(r["qty"]),
                revenue=_money(r["revenue"]),
                orders=r["orders"],
            )
            for r in rows
        ],
    )


# --------------------------------------------------------------------------------------
# list_menu_items
# --------------------------------------------------------------------------------------


def list_menu_items(conn: Conn, inp: ListMenuItemsInput) -> ListMenuItemsResult:
    ensure_restaurant(conn, inp.restaurant_id)
    rows = conn.execute(
        "SELECT id, name, category, price, active FROM menu_items "
        "WHERE restaurant_id = %s ORDER BY category, name, id",
        (inp.restaurant_id,),
    ).fetchall()
    return ListMenuItemsResult(
        restaurant_id=inp.restaurant_id,
        items=[
            MenuItem(
                menu_item_id=r["id"],
                name=r["name"],
                category=r["category"],
                price=_money(r["price"]),
                active=r["active"],
            )
            for r in rows
        ],
    )


# --------------------------------------------------------------------------------------
# reviews_for_item
# --------------------------------------------------------------------------------------

# NULL-tolerant filters: a NULL parameter disables its condition.
_REVIEWS_WHERE = (
    "r.restaurant_id = %(rid)s "
    "AND (%(item)s::int IS NULL OR r.menu_item_id = %(item)s::int) "
    "AND (%(lo)s::timestamptz IS NULL OR r.created_at >= %(lo)s::timestamptz) "
    "AND (%(hi)s::timestamptz IS NULL OR r.created_at < %(hi)s::timestamptz) "
    "AND (%(max_rating)s::int IS NULL OR r.rating <= %(max_rating)s::int)"
)


def reviews_for_item(conn: Conn, inp: ReviewsForItemInput) -> ReviewsForItemResult:
    """Reviews (most recent first) plus count/mean over *all* matching rows, not just ``limit``.

    ``menu_item_id=None`` means every review of the restaurant, including reviews not tied
    to an item. Review text is returned as ``untrusted_text``: it is customer content.
    """
    ensure_restaurant(conn, inp.restaurant_id)
    if inp.menu_item_id is not None:
        get_menu_item(conn, inp.restaurant_id, inp.menu_item_id)

    tz = local_tz()
    lo: datetime | None = None
    hi: datetime | None = None
    if inp.start_date is not None:
        lo = local_day_bounds(inp.start_date, inp.start_date)[0]
    if inp.end_date is not None:
        hi = local_day_bounds(inp.end_date, inp.end_date)[1]
    params: dict[str, Any] = {
        "rid": inp.restaurant_id,
        "item": inp.menu_item_id,
        "lo": lo,
        "hi": hi,
        "max_rating": inp.max_rating,
        "limit": inp.limit,
    }
    agg = conn.execute(
        f"SELECT count(*) AS n, avg(r.rating) AS mean FROM reviews r WHERE {_REVIEWS_WHERE}",
        params,
    ).fetchone()
    assert agg is not None
    rows = conn.execute(
        "SELECT r.id, r.menu_item_id, m.name AS item_name, r.rating, r.text, r.created_at "
        "FROM reviews r LEFT JOIN menu_items m ON m.id = r.menu_item_id "
        f"WHERE {_REVIEWS_WHERE} "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT %(limit)s",
        params,
    ).fetchall()
    reviews = [
        Review(
            review_id=r["id"],
            menu_item_id=r["menu_item_id"],
            menu_item_name=r["item_name"],
            rating=r["rating"],
            created_at=r["created_at"].astimezone(tz),
            untrusted_text=r["text"],
        )
        for r in rows
    ]
    return ReviewsForItemResult(
        restaurant_id=inp.restaurant_id,
        menu_item_id=inp.menu_item_id,
        count=agg["n"],
        mean_rating=round(float(agg["mean"]), 2) if agg["mean"] is not None else None,
        returned=len(reviews),
        reviews=reviews,
    )


# --------------------------------------------------------------------------------------
# delivery_times
# --------------------------------------------------------------------------------------

_WEEKDAY = "extract(isodow FROM o.created_at AT TIME ZONE %(tz)s)::int"
_HOUR = "extract(hour FROM o.created_at AT TIME ZONE %(tz)s)::int"
_DELIVERY_SELECT: dict[str, tuple[str, str]] = {
    # group_by -> (select expressions for weekday, hour; GROUP BY / ORDER BY clause)
    "none": ("NULL::int AS weekday, NULL::int AS hour", ""),
    "weekday": (f"{_WEEKDAY} AS weekday, NULL::int AS hour", "GROUP BY 1 ORDER BY 1"),
    "hour": (f"NULL::int AS weekday, {_HOUR} AS hour", "GROUP BY 2 ORDER BY 2"),
    "weekday_hour": (f"{_WEEKDAY} AS weekday, {_HOUR} AS hour", "GROUP BY 1, 2 ORDER BY 1, 2"),
}


def delivery_times(conn: Conn, inp: DeliveryTimesInput) -> DeliveryTimesResult:
    """Mean promised vs actual minutes. ``ratio`` is the ratio of means (actual / promised)."""
    ensure_restaurant(conn, inp.restaurant_id)
    lo, hi = local_day_bounds(inp.start_date, inp.end_date)
    select, group = _DELIVERY_SELECT[inp.group_by]
    query = sql.SQL(
        "SELECT {select}, count(*) AS n, avg(d.promised_minutes) AS promised, "
        "       avg(d.actual_minutes) AS actual "
        "FROM deliveries d JOIN orders o ON o.id = d.order_id "
        "WHERE o.restaurant_id = %(rid)s AND o.created_at >= %(lo)s AND o.created_at < %(hi)s "
        "{group}"
    ).format(select=sql.SQL(select), group=sql.SQL(group))
    rows = conn.execute(
        query, {"rid": inp.restaurant_id, "lo": lo, "hi": hi, "tz": local_tz().key}
    ).fetchall()
    groups = [
        DeliveryGroup(
            weekday=r["weekday"],
            weekday_name=WEEKDAY_NAMES[r["weekday"]] if r["weekday"] is not None else None,
            hour=r["hour"],
            deliveries=r["n"],
            mean_promised_minutes=round(float(r["promised"]), 2),
            mean_actual_minutes=round(float(r["actual"]), 2),
            ratio=round(float(r["actual"]) / float(r["promised"]), 3),
        )
        for r in rows
        if r["n"] > 0  # "none" with no deliveries returns one row of NULL averages
    ]
    return DeliveryTimesResult(
        restaurant_id=inp.restaurant_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        group_by=inp.group_by,
        groups=groups,
    )
