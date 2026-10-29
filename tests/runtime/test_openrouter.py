"""The OpenRouter adapter, against a mocked HTTP transport: no network, no key."""

import json
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import pytest

from brigada.config import Settings
from brigada.observability.cost import UnknownModelError, cost_from_usage
from brigada.runtime import agent as agent_module
from brigada.runtime.agent import Agent, AgentConfig, make_client
from brigada.runtime.openrouter import OpenRouterClient
from brigada.runtime.retry import RetryPolicy
from brigada.runtime.types import ToolCall

MODEL = "google/gemma-4-31b-it:free"


class Server:
    """Scripted OpenRouter: records every request body, answers in order."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[httpx2.Headers] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(request.headers)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def completion(
    content: str | None = None,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish: str = "stop",
    prompt_tokens: int = 120,
    completion_tokens: int = 30,
) -> httpx2.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx2.Response(
        200,
        json={
            "id": "gen-1",
            "model": MODEL,
            "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


def call(id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


def client(server: Server) -> OpenRouterClient:
    return OpenRouterClient("sk-or-test", transport=httpx2.MockTransport(server))


async def create(server: Server, **kwargs: Any) -> Any:
    params = {"model": MODEL, "max_tokens": 100, "messages": [{"role": "user", "content": "oi"}]}
    return await client(server).messages.create(**{**params, **kwargs})


# ---- request translation ----------------------------------------------------------------


async def test_conversation_with_tool_round_trip_is_translated() -> None:
    server = Server(completion("ok"))
    await create(
        server,
        system="Você é o especialista de Vendas.",
        tools=[{"name": "sales_by_period", "description": "d", "input_schema": {"type": "object"}}],
        tool_choice={"type": "tool", "name": "sales_by_period"},
        messages=[
            {"role": "user", "content": "Quanto vendi ontem?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Vou ver."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "sales_by_period",
                        "input": {"restaurant_id": 1},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "{}",
                        "is_error": False,
                    },
                ],
            },
        ],
    )
    body = server.bodies[0]
    assert body["model"] == MODEL and body["max_tokens"] == 100
    assert body["messages"] == [
        {"role": "system", "content": "Você é o especialista de Vendas."},
        {"role": "user", "content": "Quanto vendi ontem?"},
        {
            "role": "assistant",
            "content": "Vou ver.",
            "tool_calls": [call("toolu_1", "sales_by_period", '{"restaurant_id": 1}')],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "{}"},
    ]
    assert body["tools"][0] == {
        "type": "function",
        "function": {
            "name": "sales_by_period",
            "description": "d",
            "parameters": {"type": "object"},
        },
    }
    assert body["tool_choice"] == {"type": "function", "function": {"name": "sales_by_period"}}
    assert server.headers[0]["authorization"] == "Bearer sk-or-test"


async def test_tool_errors_are_marked_for_the_model() -> None:
    server = Server(completion("ok"))
    await create(
        server,
        messages=[
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "restaurant 9 does not exist",
                        "is_error": True,
                    },
                ],
            },
        ],
    )
    assert server.bodies[0]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": "ERROR: restaurant 9 does not exist",
    }


async def test_no_tools_means_no_tool_fields() -> None:
    server = Server(completion("ok"))
    await create(server, tools=[], tool_choice={"type": "tool", "name": "x"})
    assert "tools" not in server.bodies[0] and "tool_choice" not in server.bodies[0]


# ---- response translation ---------------------------------------------------------------


async def test_text_answer_is_end_turn_with_usage() -> None:
    msg = await create(Server(completion("Ontem: R$ 930,00.")))
    assert msg.stop_reason == "end_turn"
    assert msg.content[0].type == "text" and msg.content[0].text == "Ontem: R$ 930,00."
    assert (msg.usage.input_tokens, msg.usage.output_tokens) == (120, 30)


@pytest.mark.parametrize("finish", ["tool_calls", "stop"])  # some providers say "stop"
async def test_tool_calls_become_tool_use_blocks(finish: str) -> None:
    server = Server(
        completion(
            tool_calls=[call("c1", "sales_by_period", '{"restaurant_id": 1}')], finish=finish
        )
    )
    msg = await create(server)
    assert msg.stop_reason == "tool_use"
    [block] = msg.content
    assert block.type == "tool_use" and block.id == "c1" and block.input == {"restaurant_id": 1}


@pytest.mark.parametrize(
    ("finish", "stop_reason"), [("length", "max_tokens"), ("content_filter", "refusal")]
)
async def test_finish_reasons_map_to_stop_reasons(finish: str, stop_reason: str) -> None:
    assert (await create(Server(completion("x", finish=finish)))).stop_reason == stop_reason


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", "null"])
async def test_invalid_tool_arguments_are_not_trusted(raw: str) -> None:
    msg = await create(Server(completion(tool_calls=[call("c1", "sales_by_period", raw)])))
    assert set(msg.content[0].input) == {"_invalid_arguments"}  # the MCP server rejects it


# ---- errors -----------------------------------------------------------------------------


async def test_rate_limit_keeps_retry_after_for_the_retry_policy() -> None:
    limited = httpx2.Response(
        429, headers={"retry-after": "7"}, json={"error": {"message": "slow"}}
    )
    with pytest.raises(anthropic.RateLimitError) as info:
        await create(Server(limited))
    assert info.value.response.headers["retry-after"] == "7"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, anthropic.AuthenticationError),
        (400, anthropic.BadRequestError),
        (502, anthropic.InternalServerError),
        (503, anthropic.ServiceUnavailableError),
    ],
)
async def test_http_errors_map_to_sdk_errors(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        await create(Server(httpx2.Response(status, json={"error": {"message": "x"}})))


async def test_error_inside_a_200_is_still_an_error() -> None:
    body = {"error": {"code": 502, "message": "provider returned error"}}
    with pytest.raises(anthropic.InternalServerError):
        await create(Server(httpx2.Response(200, json=body)))


async def test_network_failures_are_retryable_sdk_errors() -> None:
    with pytest.raises(anthropic.APITimeoutError):
        await create(Server(httpx2.ReadTimeout("slow")))
    with pytest.raises(anthropic.APIConnectionError):
        await create(Server(httpx2.ConnectError("down")))


def test_missing_key_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterClient("")


# ---- through the agent loop -------------------------------------------------------------


async def test_agent_loop_runs_unchanged_on_openrouter() -> None:
    server = Server(
        httpx2.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow"}}),
        completion(
            tool_calls=[call("c1", "sales_by_period", '{"restaurant_id": 1}')], finish="tool_calls"
        ),
        completion("Ontem: R$ 930,00."),
    )
    seen: list[ToolCall] = []

    async def executor(c: ToolCall) -> str:
        seen.append(c)
        return '{"total_revenue": 930.0}'

    config = AgentConfig(
        name="sales",
        model=MODEL,
        system="s",
        retry=RetryPolicy(max_attempts=3, base_seconds=0, cap_seconds=0),
    )
    result = await Agent(config, client(server), [], executor).run("Quanto vendi ontem?")
    assert result.text == "Ontem: R$ 930,00."
    assert [c.input for c in seen] == [{"restaurant_id": 1}]
    assert result.cost_usd == Decimal(0)  # a :free model
    assert server.bodies[2]["messages"][-1]["role"] == "tool"


def test_make_client_follows_the_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(model_provider="openrouter", openrouter_api_key="sk-or-x")
    monkeypatch.setattr(agent_module, "get_settings", lambda: settings)
    assert isinstance(make_client(), OpenRouterClient)
    monkeypatch.setattr(agent_module, "get_settings", lambda: Settings(anthropic_api_key="sk-a"))
    assert isinstance(make_client(), anthropic.AsyncAnthropic)


def test_free_models_cost_nothing_and_unknown_paid_models_still_fail() -> None:
    usage = anthropic.types.Usage(input_tokens=1000, output_tokens=1000)
    assert cost_from_usage(MODEL, usage) == Decimal(0)
    with pytest.raises(UnknownModelError):
        cost_from_usage("google/gemma-4-31b-it", usage)


async def test_upstream_reason_is_kept_in_the_error() -> None:
    body = {
        "error": {
            "code": 429,
            "message": "Provider returned error",
            "metadata": {"raw": "model is temporarily rate-limited upstream"},
        }
    }
    with pytest.raises(anthropic.RateLimitError, match="rate-limited upstream"):
        await create(Server(httpx2.Response(429, json=body)))
