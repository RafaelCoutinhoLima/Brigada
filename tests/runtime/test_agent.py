import asyncio
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import MessageParam

from brigada.observability.cost import cost_usd
from brigada.runtime.agent import Agent, AgentConfig
from brigada.runtime.retry import RetryPolicy
from brigada.runtime.tools import ToolExecutionError
from brigada.runtime.types import (
    MaxStepsExceeded,
    ModelRefused,
    ResponseTruncated,
    RunResult,
    ToolCall,
    UnexpectedStopReason,
)

from .fakes import MODEL, FakeClient, message, system_text, text, tool_use

TOOLS: list[Any] = [{"name": "sales_by_period", "description": "d", "input_schema": {}}]


class Executor:
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[ToolCall] = []
        self.delay = delay

    async def __call__(self, call: ToolCall) -> str:
        self.calls.append(call)
        if self.delay:
            await asyncio.sleep(self.delay)
        if call.input.get("restaurant_id") == 999:
            raise ToolExecutionError("restaurant 999 does not exist")
        return '{"total_revenue": 412.3}'


def agent(client: FakeClient, executor: Executor | None = None, **cfg: Any) -> Agent:
    config = AgentConfig(
        name="sales",
        model=MODEL,
        system="Você é o especialista de vendas.",
        retry=RetryPolicy(max_attempts=2, base_seconds=0, cap_seconds=0),
        **cfg,
    )
    return Agent(config, client, TOOLS, executor or Executor())


async def test_direct_answer_is_one_step() -> None:
    client = FakeClient(message(text("Olá!")))
    result = await agent(client).run("oi")
    assert result.text == "Olá!"
    assert result.steps == 1
    assert result.tools_called == []
    req = client.messages.requests[0]
    assert req["model"] == MODEL and system_text(req).startswith("Você")
    assert req["tools"] == TOOLS
    assert req["messages"] == [{"role": "user", "content": "oi"}]


async def test_requests_mark_two_cache_breakpoints() -> None:
    client = FakeClient(message(text("Olá!")))
    await agent(client).run("oi")
    req = client.messages.requests[0]
    # The stable prefix (tools + system) and, at the top level, the growing conversation.
    assert req["system"] == [
        {
            "type": "text",
            "text": "Você é o especialista de vendas.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert req["cache_control"] == {"type": "ephemeral"}


async def test_prose_instead_of_the_terminal_tool_gets_a_reminder() -> None:
    client = FakeClient(
        message(text("O plano é olhar as vendas.")),
        message(tool_use("toolu_1", "sales_by_period", restaurant_id=1), stop_reason="tool_use"),
    )
    result = await agent(client, terminal_tools=frozenset({"sales_by_period"})).run("plano?")
    assert result.text == '{"total_revenue": 412.3}' and result.steps == 2
    reminder = client.messages.requests[1]["messages"][-1]
    assert reminder["role"] == "user" and "`sales_by_period`" in reminder["content"]
    assert "tool_choice" not in client.messages.requests[0]


async def test_a_model_that_never_calls_the_terminal_tool_fails_closed() -> None:
    client = FakeClient(*(message(text("texto")) for _ in range(2)))
    with pytest.raises(MaxStepsExceeded):
        await agent(client, terminal_tools=frozenset({"sales_by_period"}), max_steps=2).run("x")


async def test_tool_round_trip_builds_the_right_conversation() -> None:
    client = FakeClient(
        message(
            text("Vou consultar."),
            tool_use("toolu_1", "sales_by_period", restaurant_id=1),
            stop_reason="tool_use",
        ),
        message(text("Ontem você vendeu R$ 412,30.")),
    )
    executor = Executor()
    result = await agent(client, executor).run("Quanto vendi ontem?")

    assert result.text == "Ontem você vendeu R$ 412,30."
    assert result.steps == 2
    assert [c.name for c in result.tools_called] == ["sales_by_period"]
    assert executor.calls[0].input == {"restaurant_id": 1}

    second = client.messages.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[1]["content"][1] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "sales_by_period",
        "input": {"restaurant_id": 1},
    }
    assert second[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": '{"total_revenue": 412.3}',
            "is_error": False,
        }
    ]


async def test_parallel_tool_calls_run_together_and_return_in_one_message() -> None:
    client = FakeClient(
        message(
            tool_use("toolu_a", "sales_by_period", restaurant_id=1),
            tool_use("toolu_b", "sales_by_period", restaurant_id=1, group_by="channel"),
            stop_reason="tool_use",
        ),
        message(text("ok")),
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    await agent(client, Executor(delay=0.2)).run("q")
    assert loop.time() - started < 0.35  # both 0.2s tools ran concurrently

    results_msg = client.messages.requests[1]["messages"][-1]
    assert results_msg["role"] == "user"
    assert [b["tool_use_id"] for b in results_msg["content"]] == ["toolu_a", "toolu_b"]


async def test_tool_error_goes_back_to_the_model_and_loop_continues() -> None:
    client = FakeClient(
        message(tool_use("toolu_1", "sales_by_period", restaurant_id=999), stop_reason="tool_use"),
        message(text("Esse restaurante não existe.")),
    )
    result = await agent(client).run("q")
    tool_result = client.messages.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert tool_result["content"] == "restaurant 999 does not exist"
    assert result.text == "Esse restaurante não existe."


async def test_tool_timeout_is_reported_not_raised() -> None:
    client = FakeClient(
        message(tool_use("toolu_1", "sales_by_period"), stop_reason="tool_use"),
        message(text("A consulta demorou demais.")),
    )
    await agent(client, Executor(delay=1), tool_timeout_seconds=0.05).run("q")
    tool_result = client.messages.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "timed out" in tool_result["content"]


async def test_step_limit_raises() -> None:
    looping = [
        message(tool_use(f"toolu_{i}", "sales_by_period"), stop_reason="tool_use") for i in range(3)
    ]
    with pytest.raises(MaxStepsExceeded) as info:
        await agent(FakeClient(*looping), max_steps=3).run("q")
    assert info.value.max_steps == 3


async def test_max_tokens_is_an_error_not_an_answer() -> None:
    client = FakeClient(message(text("Ontem você vend"), stop_reason="max_tokens"))
    with pytest.raises(ResponseTruncated):
        await agent(client).run("q")


async def test_refusal_carries_category() -> None:
    details = {"type": "refusal", "category": "cyber", "explanation": "no"}
    client = FakeClient(message(stop_reason="refusal", stop_details=details))
    with pytest.raises(ModelRefused) as info:
        await agent(client).run("q")
    assert info.value.category == "cyber"


async def test_pause_turn_is_unexpected_here() -> None:
    with pytest.raises(UnexpectedStopReason):
        await agent(FakeClient(message(stop_reason="pause_turn"))).run("q")


async def test_tool_use_stop_without_blocks_is_unexpected() -> None:
    with pytest.raises(UnexpectedStopReason):
        await agent(FakeClient(message(text("hm"), stop_reason="tool_use"))).run("q")


async def test_usage_and_cost_accumulate_over_steps() -> None:
    client = FakeClient(
        message(tool_use("toolu_1", "sales_by_period"), stop_reason="tool_use", input_tokens=1000),
        message(text("ok"), input_tokens=1500, output_tokens=50),
    )
    result = await agent(client).run("q")
    assert result.tokens_in == 2500
    assert result.tokens_out == 70
    assert result.cost_usd == cost_usd(MODEL, 1000, 20) + cost_usd(MODEL, 1500, 50)
    assert result.cost_usd > Decimal(0)


async def test_transient_api_error_is_retried() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    overloaded = anthropic.OverloadedError(
        "overloaded", response=httpx2.Response(529, request=request), body=None
    )
    client = FakeClient(overloaded, message(text("ok")))
    result = await agent(client).run("q")
    assert result.text == "ok"
    assert len(client.messages.requests) == 2


async def test_resume_runs_pending_tools_before_calling_the_model() -> None:
    # Crash happened after the model asked for a tool and before the result was saved.
    messages: list[MessageParam] = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [tool_use("toolu_1", "sales_by_period", restaurant_id=1)]},
    ]
    executor = Executor()
    client = FakeClient(message(text("retomado")))
    result = await agent(client, executor).resume(messages, RunResult(text="", steps=1))
    assert [c.id for c in executor.calls] == ["toolu_1"]
    assert result.text == "retomado"
    assert result.steps == 2
    assert client.messages.requests[0]["messages"][-1]["content"][0]["type"] == "tool_result"


async def test_resume_refuses_a_finished_conversation() -> None:
    messages: list[MessageParam] = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [text("pronto")]},
    ]
    with pytest.raises(ValueError, match="already ended"):
        await agent(FakeClient()).resume(messages, RunResult(text="", steps=1))


async def test_fresh_budget_lets_a_resumed_run_continue() -> None:
    messages: list[MessageParam] = [{"role": "user", "content": "q"}]
    looping = [
        message(tool_use(f"toolu_{i}", "sales_by_period"), stop_reason="tool_use") for i in range(2)
    ]
    first = agent(FakeClient(*looping), max_steps=2)
    result = RunResult(text="", steps=0)
    with pytest.raises(MaxStepsExceeded):
        await first.resume(messages, result)
    assert result.steps == 2
    second = agent(FakeClient(message(text("ok"))), max_steps=2)
    with pytest.raises(MaxStepsExceeded):  # same budget: nothing left
        await second.resume(list(messages), RunResult(text="", steps=2))
    done = await second.resume(messages, result, fresh_budget=True)
    assert done.text == "ok" and done.steps == 3
