"""The real-dataset eval computes its ground truth in pandas (evals/real_truth.py), apart from
the gold tables and the tool SQL. Here both run over the same hand-written extracts and must
agree, so a disagreement on the real data points at a real bug, not at the eval."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

# pyspark (and pandas, which comes with it) live in the optional spark group: the test and db
# CI jobs do not install them, so skip before importing either.
pytest.importorskip("pyspark")
pd = pytest.importorskip("pandas")

from brigada_lakehouse import gold  # noqa: E402
from brigada_lakehouse.holidays import HOLIDAYS  # noqa: E402

from evals import real_truth  # noqa: E402
from evals.real_truth import MarginTooSmallError, Silver, top_with_margin  # noqa: E402

pytestmark = pytest.mark.spark


@pytest.fixture(scope="module")
def silver(tables: dict[str, Any]) -> Silver:
    frames = [
        tables[n].toPandas()
        for n in ("silver_orders", "silver_order_items", "silver_menu_items", "silver_restaurants")
    ]
    return Silver.from_frames(*frames)


def test_baseline_and_change_match_gold_on_every_day(
    tables: dict[str, Any], silver: Silver
) -> None:
    g = tables["gold_daily_channel_sales"].toPandas()
    daily = g.groupby(["restaurant_id", "day"]).agg(
        revenue=("revenue", "sum"),
        baseline=("baseline_revenue", lambda x: None if x.isna().all() else x.sum()),
        weeks=("baseline_weeks", "min"),
        holiday=("holiday_name", "first"),
    )
    checked = 0
    for (rid, day), row in daily.iterrows():
        if rid == 3:  # synthetic restaurants are many days: two of them are enough
            continue
        base, weeks = real_truth.baseline(silver, rid, day, HOLIDAYS)
        assert real_truth.revenue(silver, rid, day, day) == float(row.revenue), (rid, day)
        assert weeks == row.weeks, (rid, day)
        if base is None:
            assert row.baseline is None or pd.isna(row.baseline), (rid, day)
        else:  # gold rounds each channel's mean to cents
            assert abs(base - float(row.baseline)) <= 0.01 * 3, (rid, day, base, row.baseline)
        checked += 1
    assert checked > 100


def test_holiday_matching_matches_gold(tables: dict[str, Any], silver: Silver) -> None:
    gold_hol = gold.restaurant_holidays(tables["silver_restaurants"]).toPandas()
    for rid in silver.restaurants.restaurant_id:
        mine = real_truth.holidays_of(silver, rid, HOLIDAYS)
        theirs = gold_hol[gold_hol.restaurant_id == rid].set_index("day").holiday_name.to_dict()
        assert mine == theirs, rid


def test_delivery_means_match_gold(tables: dict[str, Any], silver: Silver) -> None:
    h = tables["gold_delivery_hourly"].toPandas()
    for rid in h.restaurant_id.unique():
        x = h[h.restaurant_id == rid]
        start, end = x.day.min(), x.day.max()
        d = real_truth.delivered(silver, rid, start, end)
        assert len(d) == x.delivered_orders.sum(), rid
        for column, total, count in (
            ("delivery_minutes", "delivery_minutes_sum", "timed_orders"),
            ("prep_minutes", "prep_minutes_sum", "prep_count"),
            ("rider_wait_minutes", "rider_wait_minutes_sum", "rider_wait_count"),
            ("distance_km", "distance_km_sum", "distance_count"),
        ):
            mine = real_truth.mean_of(d, column)
            n = x[count].sum()
            theirs = None if n == 0 else round(float(x[total].sum()) / n, 2)
            assert mine == theirs, (rid, column)


def test_item_quantities_match_gold(tables: dict[str, Any], silver: Silver) -> None:
    g = tables["gold_item_daily_sales"].toPandas()
    for rid in g.restaurant_id.unique():
        x = g[g.restaurant_id == rid]
        start, end = x.day.min(), x.day.max()
        mine = real_truth.item_qty(silver, rid, start, end)
        theirs = x.groupby("item_name").qty.sum()
        assert mine.sort_index().to_dict() == theirs.sort_index().to_dict(), rid


def test_ranking_refuses_a_near_tie() -> None:
    assert top_with_margin(pd.Series({"a": 138, "b": 91})) == ("a", 0.5165)
    assert top_with_margin(pd.Series({"x": -57, "y": -50}), largest=False)[0] == "x"
    assert top_with_margin(pd.Series({1: 7254.9, 2: 10256.95}), largest=False)[0] == 1
    with pytest.raises(MarginTooSmallError, match="margin"):
        top_with_margin(pd.Series({"a": -41.94, "b": -39.15}), largest=False)


def test_range_change_is_on_totals_not_a_mean_of_days(silver: Silver) -> None:
    rid = int(silver.restaurants.restaurant_id.iloc[0])
    start, end = silver.data_range(rid)
    rev, base, pct = real_truth.range_vs_baseline(silver, rid, start, end, HOLIDAYS)
    assert pct is None or pct == real_truth.change_pct(rev, base)
    assert Decimal(str(rev)) >= 0 and date(2000, 1, 1) <= start <= end
