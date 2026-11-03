"""Fixed queries over the lakehouse gold tables (ADR 0009).

Same rule as the Postgres tools (ADR 0002): the model picks the tool and its arguments, never
the SQL. Every value is a bound parameter (``:name``, Databricks SQL native parameters); the
only text interpolated is the table name, built from validated configuration identifiers.

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
    ItemTrend,
    ItemTrendsInput,
    ItemTrendsResult,
    SalesVsBaselineInput,
    SalesVsBaselineResult,
)
from brigada.mcp_server.models import WEEKDAY_NAMES

type Row = dict[str, Any]
type Fetch = Callable[[str, dict[str, Any]], list[Row]]

CHANNELS = ("app", "telefone", "balcao")


@dataclass(frozen=True)
class GoldTables:
    catalog: str
    schema: str

    def _name(self, table: str) -> str:
        return f"`{self.catalog}`.`{self.schema}`.`{table}`"

    @property
    def daily_channel_sales(self) -> str:
        return self._name("gold_daily_channel_sales")

    @property
    def item_daily_sales(self) -> str:
        return self._name("gold_item_daily_sales")


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


def _coverage(fetch: Fetch, table: str, restaurant_id: int) -> tuple[date | None, date | None]:
    rows = fetch(
        f"SELECT min(day) AS data_from, max(day) AS data_until FROM {table} "
        "WHERE restaurant_id = :restaurant_id",
        {"restaurant_id": restaurant_id},
    )
    if not rows:
        return None, None
    return _as_date(rows[0]["data_from"]), _as_date(rows[0]["data_until"])


def _missing_note(data_until: date | None, end: date) -> str | None:
    if data_until is None:
        return (
            "The lakehouse has no data for this restaurant: the export or the refresh job has "
            "not run. Use sales_by_period instead."
        )
    if data_until < end:
        return (
            f"The lakehouse has data only until {data_until.isoformat()}: later days are "
            "missing, not zero. Use sales_by_period for them."
        )
    return None


def sales_vs_baseline(
    fetch: Fetch, inp: SalesVsBaselineInput, tables: GoldTables
) -> SalesVsBaselineResult:
    table = tables.daily_channel_sales
    rows = fetch(
        "SELECT day, channel, orders, revenue, baseline_weeks, baseline_orders, "
        f"baseline_revenue FROM {table} "
        "WHERE restaurant_id = :restaurant_id AND day BETWEEN :start_date AND :end_date "
        "ORDER BY day, channel",
        {
            "restaurant_id": inp.restaurant_id,
            "start_date": inp.start_date,
            "end_date": inp.end_date,
        },
    )
    _, data_until = _coverage(fetch, table, inp.restaurant_id)

    by_day: dict[date, list[Row]] = defaultdict(list)
    for r in rows:
        if (day := _as_date(r["day"])) is not None:
            by_day[day].append(r)

    days: list[DayVsBaseline] = []
    for day in sorted(by_day):
        found = {r["channel"]: r for r in by_day[day]}
        channels = []
        for name in CHANNELS:
            r = found.get(name)
            revenue = _money(r["revenue"]) if r else 0.0
            baseline = _f(r["baseline_revenue"]) if r else None
            channels.append(
                ChannelVsBaseline(
                    channel=name,
                    orders=int(r["orders"]) if r else 0,
                    revenue=revenue,
                    baseline_orders=_f(r["baseline_orders"]) if r else None,
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
        source=f"lakehouse {table.replace('`', '')}",
        data_until=data_until,
        days=days,
        note=_missing_note(data_until, inp.end_date),
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
    data_from, data_until = _coverage(fetch, table, inp.restaurant_id)

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

    note = _missing_note(data_until, inp.end_date)
    if note is None and data_from is not None and data_from > previous_start:
        note = (
            f"The lakehouse starts on {data_from.isoformat()}, after the previous window "
            f"starts ({previous_start.isoformat()}): the previous totals are partial."
        )

    return ItemTrendsResult(
        restaurant_id=inp.restaurant_id,
        current_start=current_start,
        current_end=inp.end_date,
        previous_start=previous_start,
        previous_end=previous_end,
        order_by=inp.order_by,
        source=f"lakehouse {table.replace('`', '')}",
        data_from=data_from,
        data_until=data_until,
        total_revenue_current=total_current,
        total_revenue_previous=total_previous,
        items=items[: inp.limit],
        note=note,
    )
