"""Fixed queries over the lakehouse gold tables (ADR 0009).

Same rule as the Postgres tools (ADR 0002): the model picks the tool and its arguments, never
the SQL. Every value is a bound parameter (``:name``, Databricks SQL native parameters); the
only text interpolated is the table name, built from validated configuration identifiers, and
grouping expressions picked from a fixed map by an enum.

Functions are pure over a ``Fetch`` callable, so the tests run them with a fake and the server
with a SQL warehouse connection (``warehouse.py``).
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from brigada.lakehouse.models import (
    ChannelVsBaseline,
    DayVsBaseline,
    DeliveryGroup,
    DeliveryPerformanceInput,
    DeliveryPerformanceResult,
    ItemTrend,
    ItemTrendsInput,
    ItemTrendsResult,
    SalesVsBaselineInput,
    SalesVsBaselineResult,
)
from brigada.mcp_server.models import WEEKDAY_NAMES

type Row = dict[str, Any]
type Fetch = Callable[[str, dict[str, Any]], list[Row]]

# Display order of the channels each source has; anything else goes last, alphabetically.
CHANNEL_ORDER = ("app", "telefone", "balcao", "marketplace", "canal_proprio", "zomato_delivery")


@dataclass(frozen=True)
class GoldTables:
    catalog: str
    schema: str

    def _name(self, table: str) -> str:
        return f"`{self.catalog}`.`{self.schema}`.`{table}`"

    @property
    def restaurants(self) -> str:
        return self._name("gold_restaurants")

    @property
    def daily_channel_sales(self) -> str:
        return self._name("gold_daily_channel_sales")

    @property
    def item_daily_sales(self) -> str:
        return self._name("gold_item_daily_sales")

    @property
    def delivery_hourly(self) -> str:
        return self._name("gold_delivery_hourly")


class DuplicateRestaurantError(RuntimeError):
    """One restaurant id in two datasets: answering would mix their orders. Raised so the
    server can audit it apart from a warehouse outage."""


@dataclass(frozen=True)
class Coverage:
    data_source: str | None
    currency: str | None
    data_from: date | None
    data_until: date | None


def _f(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _money(value: Any) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _change_pct(current: float, baseline: float | None) -> float | None:
    if baseline is None or baseline <= 0:
        return None
    return round((current - baseline) / baseline * 100, 1)


def _as_date(value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _label(table: str) -> str:
    """'lakehouse gold_x': the table, never the catalog and schema (deployment detail)."""
    return f"lakehouse {table.replace('`', '').rsplit('.', 1)[-1]}"


def coverage(fetch: Fetch, tables: GoldTables, restaurant_id: int) -> Coverage:
    rows = fetch(
        f"SELECT source, currency, data_from, data_until FROM {tables.restaurants} "
        "WHERE restaurant_id = :restaurant_id",
        {"restaurant_id": restaurant_id},
    )
    if not rows:
        return Coverage(None, None, None, None)
    if len(rows) > 1:
        # Two sources mapped to one id: answering would mix their orders (and currencies).
        # Fail closed; _run_lakehouse logs it and the model gets the generic error.
        raise DuplicateRestaurantError(
            f"restaurant {restaurant_id} appears {len(rows)} times in gold"
        )
    r = rows[0]
    return Coverage(r["source"], r["currency"], _as_date(r["data_from"]), _as_date(r["data_until"]))


def _missing_note(cov: Coverage, end: date, fallback: str, start: date | None = None) -> str | None:
    if cov.data_until is None:
        return (
            "The lakehouse has no data for this restaurant: the upload or the refresh job has "
            f"not run. {fallback}"
        )
    if cov.data_from is not None and end < cov.data_from:
        return (
            f"The lakehouse has data for this restaurant only from {cov.data_from.isoformat()}: "
            "the days asked are before it, missing, not zero."
        )
    if cov.data_from is not None and start is not None and start < cov.data_from:
        return (
            f"The lakehouse has data for this restaurant only from {cov.data_from.isoformat()}: "
            "earlier days are missing, not zero."
        )
    if cov.data_until < end:
        return (
            f"The lakehouse has data for this restaurant only until "
            f"{cov.data_until.isoformat()}: later days are missing, not zero. {fallback}"
        )
    return None


def _channel_key(name: str) -> tuple[int, str]:
    return (CHANNEL_ORDER.index(name) if name in CHANNEL_ORDER else len(CHANNEL_ORDER), name)


def sales_vs_baseline(
    fetch: Fetch, inp: SalesVsBaselineInput, tables: GoldTables
) -> SalesVsBaselineResult:
    table = tables.daily_channel_sales
    rows = fetch(
        "SELECT day, channel, orders, revenue, canceled_orders, holiday_name, baseline_weeks, "
        f"baseline_orders, baseline_revenue FROM {table} "
        "WHERE restaurant_id = :restaurant_id AND day BETWEEN :start_date AND :end_date "
        "ORDER BY day, channel",
        {
            "restaurant_id": inp.restaurant_id,
            "start_date": inp.start_date,
            "end_date": inp.end_date,
        },
    )
    cov = coverage(fetch, tables, inp.restaurant_id)

    by_day: dict[date, list[Row]] = defaultdict(list)
    for r in rows:
        if (day := _as_date(r["day"])) is not None:
            by_day[day].append(r)

    days: list[DayVsBaseline] = []
    for day in sorted(by_day):
        channels = []
        for r in sorted(by_day[day], key=lambda r: _channel_key(str(r["channel"]))):
            revenue = _money(r["revenue"])
            baseline = _f(r["baseline_revenue"])
            channels.append(
                ChannelVsBaseline(
                    channel=str(r["channel"]),
                    orders=int(r["orders"] or 0),
                    revenue=revenue,
                    canceled_orders=int(r["canceled_orders"] or 0),
                    baseline_orders=_f(r["baseline_orders"]),
                    baseline_revenue=baseline,
                    revenue_change_pct=_change_pct(revenue, baseline),
                )
            )
        weeks = min(int(r["baseline_weeks"] or 0) for r in by_day[day])
        revenue = round(sum(c.revenue for c in channels), 2)
        baseline_total = (
            round(sum(c.baseline_revenue or 0.0 for c in channels), 2) if weeks > 0 else None
        )
        days.append(
            DayVsBaseline(
                day=day,
                weekday=WEEKDAY_NAMES[day.isoweekday()],
                holiday=by_day[day][0]["holiday_name"],
                orders=sum(c.orders for c in channels),
                revenue=revenue,
                canceled_orders=sum(c.canceled_orders for c in channels),
                baseline_weeks=weeks,
                baseline_revenue=baseline_total,
                revenue_change_pct=_change_pct(revenue, baseline_total),
                channels=channels,
            )
        )

    note = _missing_note(
        cov, inp.end_date, "Use sales_by_period for them if available.", inp.start_date
    )
    holidays = [f"{d.day.isoformat()} ({d.holiday})" for d in days if d.holiday]
    if holidays:
        flagged = (
            f"Holidays in this range: {', '.join(holidays)}. Their change compares a holiday "
            "with normal days: say it was a holiday instead of calling it a drop or a spike. "
            "Holidays are never part of any baseline."
        )
        note = flagged if note is None else f"{note} {flagged}"
    return SalesVsBaselineResult(
        restaurant_id=inp.restaurant_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        source=_label(table),
        data_source=cov.data_source,
        currency=cov.currency,
        data_until=cov.data_until,
        days=days,
        note=note,
    )


def item_trends(fetch: Fetch, inp: ItemTrendsInput, tables: GoldTables) -> ItemTrendsResult:
    table = tables.item_daily_sales
    current_start = inp.end_date - timedelta(days=inp.days - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=inp.days - 1)
    rows = fetch(
        "SELECT menu_item_id, max(item_name) AS name, max(category) AS category, "
        "bool_or(active) AS active, "
        "sum(CASE WHEN day >= :current_start THEN qty ELSE 0 END) AS qty_current, "
        "sum(CASE WHEN day < :current_start THEN qty ELSE 0 END) AS qty_previous, "
        "sum(CASE WHEN day >= :current_start THEN revenue ELSE 0 END) AS revenue_current, "
        "sum(CASE WHEN day < :current_start THEN revenue ELSE 0 END) AS revenue_previous, "
        "count(revenue) AS priced_rows "
        f"FROM {table} "
        "WHERE restaurant_id = :restaurant_id AND day BETWEEN :previous_start AND :end_date "
        "GROUP BY menu_item_id",
        {
            "restaurant_id": inp.restaurant_id,
            "current_start": current_start,
            "previous_start": previous_start,
            "end_date": inp.end_date,
        },
    )
    cov = coverage(fetch, tables, inp.restaurant_id)

    # Gold leaves revenue NULL on every row of a restaurant whose source has no item prices;
    # the CASE ... ELSE 0 above would turn that into 0, so priced_rows decides.
    # all([]) is True: without rows there is nothing priced, so no revenue (not a 0 total).
    priced = bool(rows) and all(int(r["priced_rows"] or 0) > 0 for r in rows)
    items = []
    for r in rows:
        qty_current, qty_previous = int(r["qty_current"] or 0), int(r["qty_previous"] or 0)
        current = _money(r["revenue_current"]) if priced else None
        previous = _money(r["revenue_previous"]) if priced else None
        items.append(
            ItemTrend(
                menu_item_id=int(r["menu_item_id"]),
                name=str(r["name"]),
                category=None if r["category"] is None else str(r["category"]),
                active=None if r["active"] is None else bool(r["active"]),
                qty_current=qty_current,
                qty_previous=qty_previous,
                qty_change=qty_current - qty_previous,
                revenue_current=current,
                revenue_previous=previous,
                revenue_change=None if current is None else round(current - (previous or 0), 2),
                revenue_change_pct=None if current is None else _change_pct(current, previous),
            )
        )
    total_current = round(sum(i.revenue_current or 0 for i in items), 2) if priced else None
    total_previous = round(sum(i.revenue_previous or 0 for i in items), 2) if priced else None

    # Without prices, rank by quantity: same meaning (fell, rose, sold most), honest unit.
    def change(i: ItemTrend) -> float:
        return i.revenue_change if i.revenue_change is not None else float(i.qty_change)

    def volume(i: ItemTrend) -> float:
        return i.revenue_current if i.revenue_current is not None else float(i.qty_current)

    if inp.order_by == "drop":
        items.sort(key=lambda i: (change(i), i.menu_item_id))
    elif inp.order_by == "rise":
        items.sort(key=lambda i: (-change(i), i.menu_item_id))
    else:
        items.sort(key=lambda i: (-volume(i), i.menu_item_id))

    if cov.data_until is not None and not rows:
        note: str | None = (
            f"This restaurant's dataset ({cov.data_source}) has no order lines: the lakehouse "
            "cannot say which dishes sold. Say so instead of guessing."
        )
    else:
        note = _missing_note(cov, inp.end_date, "Use top_items for them if available.")
    if note is None and cov.data_from is not None and cov.data_from > previous_start:
        note = (
            f"The lakehouse starts on {cov.data_from.isoformat()}, after the previous window "
            f"starts ({previous_start.isoformat()}): the previous totals are partial."
        )
    if rows and not priced:
        no_prices = (
            f"This restaurant's dataset ({cov.data_source}) has no item prices: revenue per "
            "dish is unknown (null, not zero) and items are ranked by quantity. Say so instead "
            "of estimating."
        )
        note = no_prices if note is None else f"{note} {no_prices}"

    return ItemTrendsResult(
        restaurant_id=inp.restaurant_id,
        current_start=current_start,
        current_end=inp.end_date,
        previous_start=previous_start,
        previous_end=previous_end,
        order_by=inp.order_by,
        source=_label(table),
        data_source=cov.data_source,
        currency=cov.currency,
        data_from=cov.data_from,
        data_until=cov.data_until,
        total_revenue_current=total_current,
        total_revenue_previous=total_previous,
        items=items[: inp.limit],
        note=note,
    )


# Grouping expressions by enum value: fixed text, never from the model.
_DELIVERY_KEYS = {
    "none": "'all'",
    "day": "CAST(day AS STRING)",
    "weekday": "CAST(iso_weekday AS STRING)",
    "hour": "CAST(hour AS STRING)",
    "weekday_hour": "concat(CAST(iso_weekday AS STRING), ' ', CAST(hour AS STRING))",
}


def _delivery_key(group_by: str, raw: str) -> str:
    if group_by == "weekday":
        return WEEKDAY_NAMES[int(raw)]
    if group_by == "weekday_hour":
        weekday, hour = raw.split(" ")
        return f"{WEEKDAY_NAMES[int(weekday)]} {int(hour)}"
    return raw


def _delivery_group(key: str, r: Row) -> DeliveryGroup | None:
    n = int(r["delivered_orders"] or 0)
    if n == 0:
        return None
    timed_n = int(r["timed_orders"] or 0)
    promised_n = int(r["promised_count"] or 0)
    distance_n = int(r["distance_count"] or 0)

    def phase(name: str) -> float | None:
        count = int(r[f"{name}_count"] or 0)
        return round(float(r[f"{name}_minutes_sum"]) / count, 1) if count else None

    return DeliveryGroup(
        key=key,
        delivered_orders=n,
        timed_orders=timed_n,
        mean_delivery_minutes=(
            round(float(r["delivery_minutes_sum"]) / timed_n, 1) if timed_n else None
        ),
        mean_promised_minutes=(
            round(float(r["promised_minutes_sum"]) / promised_n, 1) if promised_n else None
        ),
        late_share=round(int(r["late_orders"] or 0) / promised_n, 3) if promised_n else None,
        mean_distance_km=(
            round(float(r["distance_km_sum"]) / distance_n, 2) if distance_n else None
        ),
        prep_orders=int(r["prep_count"] or 0),
        mean_prep_minutes=phase("prep"),
        mean_pickup_minutes=phase("pickup"),
        mean_transit_minutes=phase("transit"),
        mean_rider_wait_minutes=phase("rider_wait"),
    )


_SUMS = (
    "delivered_orders",
    "timed_orders",
    "delivery_minutes_sum",
    "promised_minutes_sum",
    "promised_count",
    "late_orders",
    "distance_km_sum",
    "distance_count",
    *(
        f"{p}_{kind}"
        for p in ("prep", "pickup", "transit", "rider_wait")
        for kind in ("minutes_sum", "count")
    ),
)


def delivery_performance(
    fetch: Fetch, inp: DeliveryPerformanceInput, tables: GoldTables
) -> DeliveryPerformanceResult:
    table = tables.delivery_hourly
    sums = ", ".join(f"sum({c}) AS {c}" for c in _SUMS)
    rows = fetch(
        f"SELECT {_DELIVERY_KEYS[inp.group_by]} AS k, {sums} FROM {table} "
        "WHERE restaurant_id = :restaurant_id AND day BETWEEN :start_date AND :end_date "
        "GROUP BY k ORDER BY k",
        {
            "restaurant_id": inp.restaurant_id,
            "start_date": inp.start_date,
            "end_date": inp.end_date,
        },
    )
    cov = coverage(fetch, tables, inp.restaurant_id)

    totals: Row = {c: 0.0 for c in _SUMS}
    groups: list[DeliveryGroup] = []
    for r in rows:
        for c in _SUMS:
            totals[c] += float(r[c] or 0)
        group = _delivery_group(_delivery_key(inp.group_by, str(r["k"])), r)
        if group is not None and inp.group_by != "none":
            groups.append(group)
    if inp.group_by in ("weekday", "hour", "weekday_hour"):
        # Calendar order, not text order ("10" before "9").
        order = {name: i for i, name in WEEKDAY_NAMES.items()}

        def sort_key(g: DeliveryGroup) -> tuple[int, int]:
            parts = g.key.split(" ")
            if inp.group_by == "hour":
                return (0, int(parts[0]))
            return (order[parts[0]], int(parts[1]) if len(parts) > 1 else 0)

        groups.sort(key=sort_key)

    overall = _delivery_group("all", totals)
    note = _missing_note(
        cov, inp.end_date, "Use delivery_times for them if available.", inp.start_date
    )
    if overall is not None and overall.timed_orders == 0:
        untimed = (
            f"This restaurant's dataset ({cov.data_source}) records no delivery time: "
            "mean_delivery_minutes is unknown (null, not zero). Say so instead of estimating."
        )
        if overall.mean_prep_minutes is not None:
            untimed += (
                " It does record the kitchen time (mean_prep_minutes) and how long the courier "
                "waited for the food (mean_rider_wait_minutes)."
            )
        note = untimed if note is None else f"{note} {untimed}"
    return DeliveryPerformanceResult(
        restaurant_id=inp.restaurant_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        group_by=inp.group_by,
        source=_label(table),
        data_source=cov.data_source,
        currency=cov.currency,
        data_until=cov.data_until,
        overall=overall,
        groups=groups,
        note=note,
    )
