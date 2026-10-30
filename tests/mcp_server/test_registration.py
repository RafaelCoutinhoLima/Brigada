"""What the model can see: the registered MCP tools (no database needed)."""

import subprocess
import sys

from mcp import Client

from brigada.mcp_server.server import build_server

READ_TOOLS = {
    "sales_by_period",
    "top_items",
    "list_menu_items",
    "ratings_by_item",
    "stock_levels",
    "ingredient_consumption",
    "dish_margins",
    "cmv",
    "waste_report",
    "cost_changes",
    "reviews_for_item",
    "search_reviews",
    "delivery_times",
}
WRITE_TOOLS = {"pause_menu_item", "create_promotion", "propose_purchase"}


async def test_registered_tools_are_exactly_the_expected_set() -> None:
    async with Client(build_server()) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == READ_TOOLS | WRITE_TOOLS


async def test_human_approval_path_is_not_a_tool() -> None:
    async with Client(build_server()) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    for forbidden in ("approve", "reject", "execute", "confirm"):
        assert not any(forbidden in name for name in names), names


def test_server_module_does_not_import_the_approval_module() -> None:
    code = (
        "import sys, brigada.mcp_server.server as s; s.build_server(); "
        "print('brigada.mcp_server.actions' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "False"


async def test_annotations_mark_read_and_write_tools() -> None:
    tools = {t.name: t for t in await build_server().list_tools()}
    for name in READ_TOOLS:
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is True
    for name in WRITE_TOOLS:
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is False


async def test_schemas_expose_enums_and_required_restaurant_id() -> None:
    tools = {t.name: t for t in await build_server().list_tools()}
    for tool in tools.values():
        assert "restaurant_id" in tool.input_schema["required"], tool.name
    sales = tools["sales_by_period"].input_schema["properties"]["group_by"]
    assert sales["enum"] == ["day", "channel", "hour", "weekday", "none"]
    delivery = tools["delivery_times"].input_schema["properties"]["group_by"]
    assert delivery["enum"] == ["weekday", "hour", "weekday_hour", "none"]
    pct = tools["create_promotion"].input_schema["properties"]["discount_pct"]
    assert (pct["minimum"], pct["maximum"]) == (1, 50)
