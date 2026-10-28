import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from brigada.mcp_server.server import build_server
from brigada.runtime.mcp_tools import mcp_toolbox, stdio_server
from brigada.runtime.sales import SALES_TOOLS, ask_sales, sales_system_prompt
from brigada.runtime.tools import ToolExecutionError
from brigada.runtime.types import MaxStepsExceeded, ToolCall

from .fakes import FakeClient, message, text, tool_use

OUTAGE_DAY = "2026-10-27"


def test_stdio_child_gets_database_url_but_not_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    params = stdio_server()
    assert params.args == ["-m", "brigada.mcp_server"]
    assert params.env is not None
    assert params.env["DATABASE_URL"] == "postgresql://x@localhost/db"
    assert "ANTHROPIC_API_KEY" not in params.env


def test_system_prompt_ends_with_run_context() -> None:
    prompt = sales_system_prompt(1, date(2026, 10, 28))
    assert prompt.rstrip().endswith("- hoje: 2026-10-28 (quarta)")
    assert "restaurant_id: 1" in prompt


@pytest.mark.db
async def test_toolbox_exposes_only_allowed_tools_in_claude_format() -> None:
    async with mcp_toolbox("test-run", 1, SALES_TOOLS, build_server()) as box:
        assert sorted(t["name"] for t in box.tools) == sorted(SALES_TOOLS)
        sales = next(t for t in box.tools if t["name"] == "sales_by_period")
        assert sales["input_schema"]["type"] == "object"
        assert "restaurant_id" in sales["input_schema"]["properties"]
        assert sales["description"]


@pytest.mark.db
async def test_hidden_write_tool_cannot_be_called() -> None:
    async with mcp_toolbox("test-run", 1, SALES_TOOLS, build_server()) as box:
        call = ToolCall("toolu_1", "pause_menu_item", {"restaurant_id": 1, "menu_item_id": 101})
        with pytest.raises(ToolExecutionError, match="not available to this agent"):
            await box.executor(call)


@pytest.mark.db
async def test_executor_returns_structured_json_and_scope_is_enforced() -> None:
    args = {"start_date": OUTAGE_DAY, "end_date": OUTAGE_DAY, "group_by": "channel"}
    async with mcp_toolbox("test-run", 1, SALES_TOOLS, build_server()) as box:
        data = json.loads(
            await box.executor(ToolCall("toolu_1", "sales_by_period", {"restaurant_id": 1, **args}))
        )
        app = next(g for g in data["groups"] if g["key"] == "app")
        assert app["orders"] == 0
        with pytest.raises(ToolExecutionError, match="outside this run's scope"):
            await box.executor(ToolCall("toolu_2", "sales_by_period", {"restaurant_id": 2, **args}))


@pytest.mark.db
async def test_unknown_allowed_tool_fails_fast() -> None:
    with pytest.raises(ValueError, match="does not expose"):
        async with mcp_toolbox("test-run", 1, ["run_sql"], build_server()):
            pass


@pytest.mark.db
async def test_sales_agent_end_to_end_with_scripted_model() -> None:
    client = FakeClient(
        message(
            tool_use(
                "toolu_1",
                "sales_by_period",
                restaurant_id=1,
                start_date=OUTAGE_DAY,
                end_date=OUTAGE_DAY,
                group_by="channel",
            ),
            stop_reason="tool_use",
        ),
        message(text("Ontem (27/10) não entrou nenhum pedido pelo app.")),
    )
    result = await ask_sales(
        "Quanto vendi ontem?",
        1,
        date(2026, 10, 28),
        client=client,
        server=build_server(),
        run_id="test-e2e",
    )
    assert result.steps == 2
    tool_result = client.messages.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is False
    assert json.loads(tool_result["content"])["total_orders"] > 0
    assert "2026-10-28 (quarta)" in client.messages.requests[0]["system"]


@pytest.mark.db
async def test_stdio_transport_works() -> None:
    async with mcp_toolbox("test-run", 1, ["list_menu_items"]) as box:
        items = json.loads(
            await box.executor(ToolCall("toolu_1", "list_menu_items", {"restaurant_id": 1}))
        )
    assert len(items["items"]) > 0


@pytest.mark.db
async def test_agent_errors_come_out_unwrapped() -> None:
    looping = [
        message(tool_use(f"toolu_{i}", "list_menu_items", restaurant_id=1), stop_reason="tool_use")
        for i in range(20)
    ]
    with pytest.raises(MaxStepsExceeded):  # not an ExceptionGroup
        await ask_sales(
            "q",
            1,
            date(2026, 10, 28),
            client=FakeClient(*looping),
            server=build_server(),
            run_id="test-unwrap",
        )


def test_child_process_ignores_the_parents_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-canary\nAPI_KEYS=canary=1\n")
    params = stdio_server()
    probe = "from brigada.config import get_settings as g; s = g(); print(s.anthropic_api_key)"
    out = subprocess.run(
        [params.command, "-c", probe],
        cwd=tmp_path,
        env=params.env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "canary" not in out.stdout
