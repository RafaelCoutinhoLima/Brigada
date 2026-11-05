"""Ground truth for the real-dataset eval cases, computed in pandas from the conformed silver.

Independent of the gold tables and of the tool queries on purpose: if the lakehouse (gold
aggregation, tool SQL) and these formulas disagree, the eval catches it instead of grading the
tools against themselves. Shared with the pipeline: only the silver (the cleaning rules, tested
on their own) and the holiday *data* (``brigada_lakehouse.holidays.HOLIDAYS``); the holiday
matching and the baseline below are written again.

Conventions, the same the tools document: days are the restaurant's local calendar days,
inclusive; revenue counts completed orders; the baseline of a day is the mean revenue of the
previous 4 same weekdays that were not holidays for the restaurant, at most 8 weeks back, a day
without orders counting 0 when it is inside the restaurant's data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

BASELINE_WEEKS = 4
LOOKBACK_WEEKS = 8
MIN_MARGIN = 0.10  # an entity answer needs the 1st to beat the 2nd by 10%
MIN_GROUP = 20  # a group needs 20 orders to be "the slowest hour"


class MarginTooSmallError(ValueError):
    """The 1st and 2nd places are too close for a fair entity question."""


@dataclass(frozen=True)
class Silver:
    orders: pd.DataFrame  # silver_orders
    order_items: pd.DataFrame
    menu_items: pd.DataFrame
    restaurants: pd.DataFrame  # silver_restaurants

    @classmethod
    def from_frames(
        cls,
        orders: pd.DataFrame,
        order_items: pd.DataFrame,
        menu_items: pd.DataFrame,
        restaurants: pd.DataFrame,
    ) -> Silver:
        o = orders.copy()
        o["local_day"] = pd.to_datetime(o["local_day"]).dt.date
        o["total"] = o["total"].astype(float)
        return cls(o, order_items.copy(), menu_items.copy(), restaurants.copy())

    def of(self, rid: int, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        o = self.orders[self.orders.restaurant_id == rid]
        if start is not None:
            o = o[o.local_day >= start]
        if end is not None:
            o = o[o.local_day <= end]
        return o

    def data_range(self, rid: int) -> tuple[date, date]:
        days = self.of(rid).local_day
        return days.min(), days.max()


# Sales ------------------------------------------------------------------------------------


def revenue(s: Silver, rid: int, start: date, end: date, channel: str | None = None) -> float:
    o = s.of(rid, start, end)
    o = o[o.status == "completed"]
    if channel is not None:
        o = o[o.channel == channel]
    return round(float(o.total.sum()), 2)


def completed_orders(s: Silver, rid: int, start: date, end: date) -> int:
    o = s.of(rid, start, end)
    return int((o.status == "completed").sum())


def canceled_orders(s: Silver, rid: int, start: date, end: date) -> int:
    o = s.of(rid, start, end)
    return int((o.status == "canceled").sum())


def holidays_of(s: Silver, rid: int, holidays: Sequence) -> dict[date, str]:
    """The holidays that apply to the restaurant: same country; a holiday's state and city,
    when set, equal to the restaurant's (so a Rio holiday never reaches a restaurant without a
    state, even one in a city called Rio de Janeiro)."""
    r = s.restaurants[s.restaurants.restaurant_id == rid].iloc[0]
    state = None if pd.isna(r["state"]) else str(r["state"]).upper()  # NULL arrives as NaN
    city = None if pd.isna(r["city"]) else str(r["city"]).lower()
    out: dict[date, list[str]] = {}
    for h in holidays:
        if h.country != r["country"]:
            continue
        if h.state is not None and h.state.upper() != state:
            continue
        if h.city is not None and h.city.lower() != city:
            continue
        out.setdefault(h.day, []).append(h.name)
    return {d: " / ".join(sorted(names)) for d, names in out.items()}


def baseline(s: Silver, rid: int, day: date, holidays: Sequence) -> tuple[float | None, int]:
    """(mean revenue of the previous normal same weekdays, how many were used)."""
    first, _ = s.data_range(rid)
    off = holidays_of(s, rid, holidays)
    used: list[float] = []
    for k in range(1, LOOKBACK_WEEKS + 1):
        past = day - timedelta(weeks=k)
        if past < first:
            break
        if past in off:
            continue
        used.append(revenue(s, rid, past, past))
        if len(used) == BASELINE_WEEKS:
            break
    if not used:
        return None, 0
    return sum(used) / len(used), len(used)


def change_pct(current: float, base: float | None) -> float | None:
    if base is None or base <= 0:
        return None
    return round((current - base) / base * 100, 2)


def range_vs_baseline(
    s: Silver, rid: int, start: date, end: date, holidays: Sequence
) -> tuple[float, float, float | None]:
    """(revenue, summed baseline, change %) over a range: totals first, then the %, never a
    mean of daily percentages."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    rev = sum(revenue(s, rid, d, d) for d in days)
    bases = [baseline(s, rid, d, holidays)[0] for d in days]
    base = sum(b for b in bases if b is not None)
    return round(rev, 2), round(base, 2), change_pct(rev, base)


# Delivery ---------------------------------------------------------------------------------


def delivered(s: Silver, rid: int, start: date, end: date) -> pd.DataFrame:
    """Completed orders known to be delivered: a delivery time or a distance."""
    o = s.of(rid, start, end)
    o = o[o.status == "completed"]
    return o[o.delivery_minutes.notna() | o.distance_km.notna()]


def mean_of(df: pd.DataFrame, column: str) -> float | None:
    values = df[column].dropna()
    return None if values.empty else round(float(values.mean()), 2)


def slowest_group(
    s: Silver, rid: int, start: date, end: date, by: list[str], column: str = "delivery_minutes"
) -> tuple[tuple, float, float]:
    """(group key, its mean, margin over the 2nd) among groups with >= MIN_GROUP values."""
    d = delivered(s, rid, start, end).dropna(subset=[column])
    g = d.groupby(by)[column].agg(["mean", "count"])
    g = g[g["count"] >= MIN_GROUP].sort_values("mean", ascending=False)
    key, margin = top_with_margin(g["mean"])
    return key if isinstance(key, tuple) else (key,), round(float(g["mean"].iloc[0]), 2), margin


# Items ------------------------------------------------------------------------------------


def item_qty(s: Silver, rid: int, start: date, end: date) -> pd.Series:
    """Quantity per dish name over completed orders, every dish of the menu (0 included)."""
    o = s.of(rid, start, end)
    ids = o[o.status == "completed"].order_id
    lines = s.order_items[s.order_items.order_id.isin(ids)]
    qty = lines.groupby("menu_item_id").qty.sum()
    menu = s.menu_items[s.menu_items.restaurant_id == rid].set_index("menu_item_id").name
    return qty.reindex(menu.index, fill_value=0).rename(index=menu).astype(int)


def item_change(s: Silver, rid: int, end: date, days: int) -> pd.Series:
    """qty in the last ``days`` days up to ``end`` minus the ``days`` before."""
    cur_start = end - timedelta(days=days - 1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return item_qty(s, rid, cur_start, end) - item_qty(s, rid, prev_start, prev_end)


# Ranking ----------------------------------------------------------------------------------


def top_with_margin(values: pd.Series, *, largest: bool = True) -> tuple[object, float]:
    """(key of the 1st place, margin = |1st - 2nd| / min(|1st|, |2nd|)), the relative gap
    whichever the direction (largest, smallest, most negative). Raises when the margin is below
    MIN_MARGIN: a near tie is not a fair question, and the case must be rewritten, not kept."""
    ordered = values.sort_values(ascending=not largest)
    if len(ordered) < 2:
        raise MarginTooSmallError(f"need 2 candidates, got {len(ordered)}")
    first, second = float(ordered.iloc[0]), float(ordered.iloc[1])
    smaller = min(abs(first), abs(second))
    if smaller == 0:
        margin = float("inf") if first != second else 0.0
    else:
        margin = abs(first - second) / smaller
    if margin < MIN_MARGIN:
        raise MarginTooSmallError(
            f"{ordered.index[0]!r} ({first:.2f}) vs {ordered.index[1]!r} ({second:.2f}): "
            f"margin {margin:.1%} < {MIN_MARGIN:.0%}"
        )
    return ordered.index[0], round(margin, 4)
