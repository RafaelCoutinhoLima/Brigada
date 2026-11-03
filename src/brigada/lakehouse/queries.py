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
CHANNEL_ORDER = ("app", "telefone", "balcao", "marketplace", "canal_proprio")


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


@dataclass(frozen=True)
class Coverage:
    data_source: str | None
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
    return f"lakehouse {table.replace('`', '')}"


def coverage(fetch: Fetch, tables: GoldTables, restaurant_id: int) -> Coverage:
    rows = fetch(
        f"SELECT source, data_from, data_until FROM {tables.restaurants} "
        "WHERE restaurant_id = :restaurant_id",
        {"restaurant_id": restaurant_id},
    )
    if not rows:
        return Coverage(None, None, None)
    r = rows[0]
    return Coverage(r["source"], _as_date(r["data_from"]), _as_date(r["data_until"]))


def _missing_note(cov: Coverage, end: date, fallback: str) -> str | None:
    if cov.data_until is None:
        return (
            "The lakehouse has no data for this restaurant: the upload or the refresh job has "
            f"not run. {fallback}"
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
        "SELECT day, channel, orders, revenue, canceled_orders, baseline_weeks, "
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
                orders=sum(c.orders for c in channels),
                revenue=revenue,
                canceled_orders=sum(c.canceled_orders for c in channels),
                baseline_weeks=weeks,
                baseline_revenue=baseline_total,
                revenue_change_pct=_change_pct(revenue, baseline_total),
                channels=channels,
            )
        )

    return SalesVsBaselineResult(
        restaurant_id=inp.restaurant_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        source=_label(table),
        data_source=cov.data_source,
        data_until=cov.data_until,
        days=days,
        note=_missing_note(cov, inp.end_date, "Use sales_by_period for them if available."),
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
        "sum(CASE WHEN day < :current_start THEN revenue ELSE 0 END) AS revenue_previous "
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

    items = []
    for r in rows:
        current, previous = _money(r["revenue_current"]), _money(r["revenue_previous"])
        items.append(
            ItemTrend(
                menu_item_id=int(r["menu_item_id"]),
                name=str(r["name"]),
                category=str(r["category"]),
                active=bool(r["active"]),
                qty_current=int(r["qty_current"] or 0),
                qty_previous=int(r["qty_previous"] or 0),
                revenue_current=current,
                revenue_previous=previous,
                revenue_change=round(current - previous, 2),
                revenue_change_pct=_change_pct(current, previous),
            )
        )
    total_current = round(sum(i.revenue_current for i in items), 2)
    total_previous = round(sum(i.revenue_previous for i in items), 2)

    if inp.order_by == "drop":
        items.sort(key=lambda i: (i.revenue_change, i.menu_item_id))
    elif inp.order_by == "rise":
        items.sort(key=lambda i: (-i.revenue_change, i.menu_item_id))
    else:
        items.sort(key=lambda i: (-i.revenue_current, i.menu_item_id))

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

    return ItemTrendsResult(
        restaurant_id=inp.restaurant_id,
        current_start=current_start,
        current_end=inp.end_date,
        previous_start=previous_start,
        previous_end=previous_end,
        order_by=inp.order_by,
        source=_label(table),
        data_source=cov.data_source,
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
    promised_n = int(r["promised_count"] or 0)
    distance_n = int(r["distance_count"] or 0)
    return DeliveryGroup(
        key=key,
        delivered_orders=n,
        mean_delivery_minutes=round(float(r["delivery_minutes_sum"]) / n, 1),
        mean_promised_minutes=(
            round(float(r["promised_minutes_sum"]) / promised_n, 1) if promised_n else None
        ),
        late_share=round(int(r["late_orders"] or 0) / promised_n, 3) if promised_n else None,
        mean_distance_km=(
            round(float(r["distance_km_sum"]) / distance_n, 2) if distance_n else None
        ),
    )


_SUMS = (
    "delivered_orders",
    "delivery_minutes_sum",
    "promised_minutes_sum",
    "promised_count",
    "late_orders",
    "distance_km_sum",
    "distance_count",
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

    return DeliveryPerformanceResult(
        restaurant_id=inp.restaurant_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        group_by=inp.group_by,
        source=_label(table),
        data_source=cov.data_source,
        data_until=cov.data_until,
        overall=_delivery_group("all", totals),
        groups=groups,
        note=_missing_note(cov, inp.end_date, "Use delivery_times for them if available."),
    )
