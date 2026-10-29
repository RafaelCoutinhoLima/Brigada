"""MCP tool output vs. the pure data generator, an oracle independent of the SQL.

The generator knows the local (America/Sao_Paulo) time of every order, so these tests catch
timezone bugs in the queries that sums and group counts alone do not.
"""

from collections import Counter
from datetime import date, timedelta
from functools import cache
from typing import Any

import pytest

from brigada.mcp_server.server import BrigadaMCPServer
from scripts.seed_data import Dataset, generate

from .conftest import END_DATE, START_DATE, scoped_call

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("run_id")]


@cache
def dataset() -> Dataset:
    return generate(END_DATE)


async def call(server: BrigadaMCPServer, name: str, **args: Any) -> dict[str, Any]:
    result = await scoped_call(server, name, args)
    assert not result.is_error and isinstance(result.structured_content, dict)
    return result.structured_content


def orders(rid: int, start: date, end: date) -> list[Any]:
    return [
        o
        for o in dataset().orders
        if o.restaurant_id == rid and start <= o.created_at.date() <= end
    ]


async def test_hour_buckets_use_local_time(server: BrigadaMCPServer) -> None:
    out = await call(
        server,
        "sales_by_period",
        restaurant_id=1,
        start_date=START_DATE.isoformat(),
        end_date=END_DATE.isoformat(),
        group_by="hour",
    )
    expected = Counter(o.created_at.hour for o in orders(1, START_DATE, END_DATE))
    got = {int(g["key"]): g["orders"] for g in out["groups"] if g["orders"]}
    assert got == dict(expected)
    assert got.get(21, 0) > 0 and got.get(0, 0) == 0  # dinner stays at night, not at 0h UTC


async def test_day_buckets_use_local_calendar_days(server: BrigadaMCPServer) -> None:
    start = END_DATE - timedelta(days=13)
    out = await call(
        server,
        "sales_by_period",
        restaurant_id=2,
        start_date=start.isoformat(),
        end_date=END_DATE.isoformat(),
        group_by="day",
    )
    expected = Counter(o.created_at.date().isoformat() for o in orders(2, start, END_DATE))
    got = {g["key"]: g["orders"] for g in out["groups"] if g["orders"]}
    assert got == dict(expected)


async def test_late_share_matches_generator(server: BrigadaMCPServer) -> None:
    start = END_DATE - timedelta(days=29)
    out = await call(
        server,
        "delivery_times",
        restaurant_id=2,
        start_date=start.isoformat(),
        end_date=END_DATE.isoformat(),
        group_by="none",
    )
    ids = {o.id for o in orders(2, start, END_DATE)}
    ds = [d for d in dataset().deliveries if d.order_id in ids]
    late = sum(1 for d in ds if d.actual_minutes > d.promised_minutes)
    [group] = out["groups"]
    assert group["deliveries"] == len(ds)
    assert group["late_deliveries"] == late
    assert group["late_share"] == pytest.approx(late / len(ds), abs=1e-4)


async def test_delivery_hour_buckets_use_local_time(server: BrigadaMCPServer) -> None:
    out = await call(
        server,
        "delivery_times",
        restaurant_id=3,
        start_date=START_DATE.isoformat(),
        end_date=END_DATE.isoformat(),
        group_by="hour",
    )
    hour_of = {o.id: o.created_at.hour for o in orders(3, START_DATE, END_DATE)}
    expected = Counter(hour_of[d.order_id] for d in dataset().deliveries if d.order_id in hour_of)
    assert {g["hour"]: g["deliveries"] for g in out["groups"]} == dict(expected)


async def test_ratings_by_item_match_the_generator(server: BrigadaMCPServer) -> None:
    out = await call(server, "ratings_by_item", restaurant_id=3, min_reviews=5)
    ratings: dict[int, list[int]] = {}
    for r in dataset().reviews:
        if r.restaurant_id == 3 and r.menu_item_id is not None:
            ratings.setdefault(r.menu_item_id, []).append(r.rating)
    expected = {i: (len(v), round(sum(v) / len(v), 2)) for i, v in ratings.items() if len(v) >= 5}
    got = {i["menu_item_id"]: (i["reviews"], i["mean_rating"]) for i in out["items"]}
    assert got == expected
    means = [i["mean_rating"] for i in out["items"]]
    assert means == sorted(means, reverse=True)  # best first


async def test_ratings_by_item_answers_the_eval_question(server: BrigadaMCPServer) -> None:
    # menu-008: "Entre os itens com pelo menos 5 avaliações, qual tem a melhor nota?"
    out = await call(server, "ratings_by_item", restaurant_id=3, min_reviews=5)
    assert out["items"][0]["name"] == "Missoshiru"
    assert all(i["reviews"] >= 5 for i in out["items"])


async def test_days_with_orders_count_real_days_not_calendar(server: BrigadaMCPServer) -> None:
    # A 6-month window over a 90-day history: averages must use the days that have data.
    out = await call(
        server,
        "sales_by_period",
        restaurant_id=1,
        start_date="2026-05-01",
        end_date=END_DATE.isoformat(),
        group_by="weekday",
    )
    tuesdays = {
        o.created_at.date()
        for o in orders(1, START_DATE, END_DATE)
        if o.created_at.isoweekday() == 2
    }
    group = next(g for g in out["groups"] if g["key"] == "tuesday")
    assert group["days_with_orders"] == len(tuesdays) < 26  # not the 26 calendar Tuesdays
    all_days = {o.created_at.date() for o in orders(1, START_DATE, END_DATE)}
    assert out["days_with_orders"] == len(all_days)
