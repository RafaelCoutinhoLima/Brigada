"""Inventory tools vs the pure generator (an oracle independent of the SQL)."""

from datetime import date, timedelta
from decimal import Decimal
from functools import cache
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from brigada.mcp_server import actions
from brigada.mcp_server.db import Conn
from brigada.mcp_server.errors import InvalidStateError, NotFoundError
from brigada.mcp_server.server import BrigadaMCPServer
from scripts.seed_data import Dataset, generate
from scripts.seed_inventory import Inventory, dish_cost, generate_inventory, stock_on_hand

from .conftest import END_DATE, START_DATE, scoped_call

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("run_id")]
PERIOD = {"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()}


@cache
def world() -> tuple[Dataset, Inventory]:
    ds = generate(END_DATE)
    return ds, generate_inventory(ds)


def ingredient(rid: int, name: str) -> Any:
    return next(i for i in world()[1].ingredients if i.restaurant_id == rid and i.name == name)


async def call(server: BrigadaMCPServer, name: str, **args: Any) -> dict[str, Any]:
    result = await scoped_call(server, name, args)
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def test_mozzarella_runs_out_before_the_supplier_delivers(server: BrigadaMCPServer) -> None:
    out = await call(server, "stock_levels", restaurant_id=1)
    first = out["items"][0]  # lowest cover first
    assert first["name"] == "Mussarela"
    assert first["runs_out_before_delivery"] and first["cover_days"] == pytest.approx(2, abs=0.1)
    assert [i["name"] for i in out["items"] if i["runs_out_before_delivery"]] == ["Mussarela"]
    moz = ingredient(1, "Mussarela")
    assert first["on_hand"] == pytest.approx(float(stock_on_hand(world()[1])[moz.id]), abs=1e-3)
    below = await call(server, "stock_levels", restaurant_id=1, only_below_reorder_point=True)
    assert all(i["below_reorder_point"] for i in below["items"]) and below["items"]


async def test_margherita_has_the_lowest_margin_among_mains(server: BrigadaMCPServer) -> None:
    out = await call(
        server,
        "dish_margins",
        restaurant_id=1,
        category="principal",
        order_by="margin_pct",
        **PERIOD,
    )
    assert out["items"][0]["name"] == "Pizza margherita"
    ds, inv = world()
    for item in out["items"]:
        expected = dish_cost(inv, item["menu_item_id"], START_DATE, END_DATE)
        assert item["unit_cost"] == pytest.approx(float(expected), abs=0.01)
        assert item["contribution"] == pytest.approx(item["margin"] * item["qty_sold"], abs=0.05)


async def test_temaki_margin_falls_with_salmon(server: BrigadaMCPServer) -> None:
    last30 = END_DATE - timedelta(days=29)
    before = await call(
        server,
        "dish_margins",
        restaurant_id=3,
        start_date=START_DATE.isoformat(),
        end_date=(last30 - timedelta(days=1)).isoformat(),
    )
    after = await call(
        server,
        "dish_margins",
        restaurant_id=3,
        start_date=last30.isoformat(),
        end_date=END_DATE.isoformat(),
    )
    m = {x["name"]: x["margin_pct"] for x in before["items"]}["Temaki de salmão"]
    n = {x["name"]: x["margin_pct"] for x in after["items"]}["Temaki de salmão"]
    assert m - n > 0.08 and 0.42 < n < 0.52
    changes = await call(server, "cost_changes", restaurant_id=3, recent_days=30)
    assert changes["items"][0]["name"] == "Salmão"
    assert changes["items"][0]["change_pct"] == pytest.approx(0.30, abs=0.01)


async def test_beans_lose_fifteen_percent(server: BrigadaMCPServer) -> None:
    out = await call(server, "waste_report", restaurant_id=2)
    beans = next(i for i in out["items"] if i["name"] == "Feijão preto")
    assert beans["loss_share"] == pytest.approx(0.15, abs=0.005)
    assert all(i["loss_share"] <= 0.02 for i in out["items"] if i["name"] != "Feijão preto")
    assert out["total_loss_value"] == pytest.approx(sum(i["loss_value"] for i in out["items"]))


async def test_cmv_matches_recipes_times_items_sold(server: BrigadaMCPServer) -> None:
    out = await call(server, "cmv", restaurant_id=1, group_by="category", **PERIOD)
    ds, inv = world()
    start, end = START_DATE, END_DATE
    in_period = {o.id for o in ds.orders if o.restaurant_id == 1}
    revenue = sum(
        (i.qty * i.unit_price for i in ds.order_items if i.order_id in in_period), Decimal(0)
    )
    cogs = sum(
        (
            i.qty * dish_cost(inv, i.menu_item_id, start, end)
            for i in ds.order_items
            if i.order_id in in_period
        ),
        Decimal(0),
    )
    assert out["revenue"] == pytest.approx(float(revenue), abs=0.05)
    assert out["cost_of_goods"] == pytest.approx(float(cogs), abs=0.5)
    assert out["cmv_pct"] == pytest.approx(float(cogs / revenue), abs=1e-3)
    assert sum(g["revenue"] for g in out["groups"]) == pytest.approx(out["revenue"], abs=0.05)


async def test_consumption_matches_sales_movements(server: BrigadaMCPServer) -> None:
    moz = ingredient(1, "Mussarela")
    out = await call(
        server, "ingredient_consumption", restaurant_id=1, ingredient_id=moz.id, **PERIOD
    )
    [line] = out["items"]
    sold = -sum(
        m.qty for m in world()[1].movements if m.ingredient_id == moz.id and m.kind == "sale"
    )
    assert line["sold"] == pytest.approx(float(sold), abs=1e-3)


async def test_purchase_is_only_a_proposal_until_a_human_approves(
    server: BrigadaMCPServer, db: Conn
) -> None:
    moz = ingredient(1, "Mussarela")
    before = db.execute("SELECT count(*) AS n FROM stock_movements").fetchone()
    out = await call(
        server,
        "propose_purchase",
        restaurant_id=1,
        ingredient_id=moz.id,
        qty=20,
        reason="cobertura de 2 dias",
    )
    assert out["kind"] == "propose_purchase" and out["status"] == "pending"
    assert db.execute("SELECT count(*) AS n FROM stock_movements").fetchone() == before
    decided = actions.approve(out["action_id"], approved_by="gerente")
    assert decided.status == "executed"


async def test_purchase_of_another_restaurants_ingredient_is_refused(
    server: BrigadaMCPServer,
) -> None:
    salmon = ingredient(3, "Salmão")
    with pytest.raises(ToolError, match="does not exist in restaurant 1"):
        await scoped_call(
            server,
            "propose_purchase",
            {"restaurant_id": 1, "ingredient_id": salmon.id, "qty": 5, "reason": "reposição"},
        )


def test_period_helper_is_the_eval_period() -> None:
    assert START_DATE == date(2026, 7, 30) and END_DATE == date(2026, 10, 27)


async def test_approval_rechecks_the_ingredient_belongs_to_the_restaurant(
    server: BrigadaMCPServer, db: Conn
) -> None:
    # A payload changed after the proposal (or a bug upstream) must not approve across tenants.
    out = await call(
        server,
        "propose_purchase",
        restaurant_id=1,
        ingredient_id=ingredient(1, "Mussarela").id,
        qty=5,
        reason="reposição",
    )
    salmon = ingredient(3, "Salmão")
    db.execute(
        "UPDATE pending_actions SET payload = jsonb_set(payload, '{ingredient_id}', %s::jsonb) "
        "WHERE id = %s",
        (str(salmon.id), out["action_id"]),
    )
    with pytest.raises(InvalidStateError, match="target no longer exists") as exc:
        actions.approve(out["action_id"], approved_by="gerente")
    # The tenant guard is what refused it.
    assert isinstance(exc.value.__cause__, NotFoundError)
    assert "does not exist in restaurant 1" in str(exc.value.__cause__)


async def test_stock_levels_only_lists_the_restaurants_own_ingredients(
    server: BrigadaMCPServer,
) -> None:
    for rid in (1, 2, 3):
        out = await call(server, "stock_levels", restaurant_id=rid)
        own = {i.id for i in world()[1].ingredients if i.restaurant_id == rid}
        assert {i["ingredient_id"] for i in out["items"]} == own


@pytest.mark.parametrize("category", ["None", "pratos", "Principal"])
async def test_an_unknown_category_is_an_error_not_an_empty_list(
    server: BrigadaMCPServer, category: str
) -> None:
    """Eval inv-010: a model sent category="None" and read the empty result as "no margins"."""
    with pytest.raises(ToolError, match="category"):
        await call(server, "dish_margins", restaurant_id=3, category=category, **PERIOD)
