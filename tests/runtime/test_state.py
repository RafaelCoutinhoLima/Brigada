import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import anthropic
import httpx2
import psycopg
import pytest
from anthropic.types import MessageParam
from psycopg.rows import dict_row

from brigada.mcp_server.server import build_server
from brigada.runtime.agent import Agent, AgentConfig
from brigada.runtime.retry import RetryPolicy
from brigada.runtime.sales import ask_sales, resume_sales
from brigada.runtime.state import RunStore, resume_persisted, run_persisted
from brigada.runtime.types import MaxStepsExceeded, ResponseTruncated, ToolCall

from .fakes import MODEL, FakeClient, message, system_text, text, tool_use

pytestmark = pytest.mark.db

TODAY = date(2026, 10, 28)
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.fixture
async def store() -> AsyncIterator[RunStore]:
    url = os.environ["DATABASE_URL"]
    yield RunStore(url)
    with psycopg.connect(url) as conn:
        conn.execute("DELETE FROM runs WHERE id LIKE 'test-%'")


def rows(sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


class Executor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def __call__(self, call: ToolCall) -> str:
        self.calls.append(call)
        return '{"total_revenue": 412.3}'


def make_agent(client: FakeClient, executor: Executor) -> Agent:
    cfg = AgentConfig(
        name="sales",
        model=MODEL,
        system="s",
        retry=RetryPolicy(max_attempts=1, base_seconds=0, cap_seconds=0),
    )
    return Agent(cfg, client, [], executor)


def run_id() -> str:
    return f"test-{uuid.uuid4().hex}"


def bad_request() -> anthropic.BadRequestError:
    return anthropic.BadRequestError(
        "invalid", response=httpx2.Response(400, request=REQUEST), body=None
    )


async def start(agent: Agent, store: RunStore, rid: str) -> Any:
    return await run_persisted(
        agent,
        store,
        rid,
        restaurant_id=1,
        question="Quanto vendi ontem?",
        today=TODAY,
        prompt_sha256="abc123",
    )


async def test_successful_run_is_saved_step_by_step(store: RunStore) -> None:
    rid = run_id()
    client = FakeClient(
        message(tool_use("toolu_1", "sales_by_period"), stop_reason="tool_use", input_tokens=1000),
        message(text("R$ 412,30"), input_tokens=1200, output_tokens=30),
    )
    result = await start(make_agent(client, Executor()), store, rid)

    [run] = rows("SELECT * FROM runs WHERE id = %s", rid)
    assert run["status"] == "succeeded" and run["answer"] == "R$ 412,30"
    assert run["today"] == TODAY and run["prompt_sha256"] == "abc123"
    assert (run["steps"], run["tokens_in"], run["tokens_out"]) == (2, 2200, 50)
    assert run["cost_usd"] == result.cost_usd
    assert [m["role"] for m in run["messages"]] == ["user", "assistant", "user", "assistant"]

    steps = rows("SELECT step, kind FROM steps WHERE run_id = %s ORDER BY id", rid)
    assert [(s["step"], s["kind"]) for s in steps] == [
        (1, "llm_call"),
        (1, "tool_results"),
        (2, "llm_call"),
    ]


async def test_failed_run_resumes_from_last_checkpoint(store: RunStore) -> None:
    rid = run_id()
    executor = Executor()
    first = FakeClient(
        message(tool_use("toolu_1", "sales_by_period"), stop_reason="tool_use", input_tokens=1000),
        bad_request(),
    )
    with pytest.raises(anthropic.BadRequestError):
        await start(make_agent(first, executor), store, rid)
    [run] = rows("SELECT status, error, steps FROM runs WHERE id = %s", rid)
    assert run["status"] == "failed" and run["error"].startswith("BadRequestError")
    assert run["steps"] == 1

    second = FakeClient(message(text("R$ 412,30"), input_tokens=1200, output_tokens=30))
    record = await store.load(rid)
    result = await resume_persisted(
        make_agent(second, executor), store, record, prompt_sha256="abc123"
    )

    assert result.text == "R$ 412,30"
    assert result.steps == 2
    assert (result.tokens_in, result.tokens_out) == (2200, 50)  # counters carried over
    assert [c.id for c in result.tools_called] == ["toolu_1"]
    assert len(executor.calls) == 1  # the tool that already ran was NOT run again
    resumed_with = second.messages.requests[0]["messages"]
    assert resumed_with[-1]["content"][0]["type"] == "tool_result"
    [run] = rows("SELECT status, error, steps FROM runs WHERE id = %s", rid)
    assert (run["status"], run["error"], run["steps"]) == ("succeeded", None, 2)


async def test_crash_between_model_and_tools_runs_the_pending_tools(store: RunStore) -> None:
    # Process died right after saving the model's tool request: status is still 'running'.
    rid = run_id()
    messages: list[MessageParam] = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [tool_use("toolu_9", "sales_by_period")]},
    ]
    await store.create(
        rid,
        agent="sales",
        restaurant_id=1,
        question="q",
        today=TODAY,
        model=MODEL,
        prompt_sha256="abc123",
        messages=messages[:1],
    )
    await store.checkpoint(rid, step=1, kind="llm_call", payload={}, messages=messages)

    executor = Executor()
    record = await store.load(rid)
    assert record.status == "running" and record.result.tools_called == []
    result = await resume_persisted(
        make_agent(FakeClient(message(text("ok"))), executor), store, record, prompt_sha256="abc123"
    )
    assert [c.id for c in executor.calls] == ["toolu_9"]
    assert result.steps == 2


async def test_succeeded_run_cannot_be_resumed(store: RunStore) -> None:
    rid = run_id()
    await start(make_agent(FakeClient(message(text("ok"))), Executor()), store, rid)
    with pytest.raises(ValueError, match="already succeeded"):
        await resume_persisted(
            make_agent(FakeClient(), Executor()), store, await store.load(rid), prompt_sha256="x"
        )


async def test_checkpoint_refused_after_run_finished(store: RunStore) -> None:
    rid = run_id()
    await start(make_agent(FakeClient(message(text("ok"))), Executor()), store, rid)
    with pytest.raises(RuntimeError, match="not running"):
        await store.checkpoint(rid, step=9, kind="llm_call", payload={}, messages=[])


async def test_unknown_run(store: RunStore) -> None:
    with pytest.raises(LookupError):
        await store.load("test-does-not-exist")


async def test_sales_run_resumes_with_original_date_and_restaurant(store: RunStore) -> None:
    rid = run_id()
    with pytest.raises(anthropic.BadRequestError):
        await ask_sales(
            "Quanto vendi ontem?",
            1,
            TODAY,
            client=FakeClient(bad_request()),
            server=build_server(),
            run_id=rid,
            store=store,
        )
    second = FakeClient(message(text("ok")))
    await resume_sales(rid, store, client=second, server=build_server())
    assert "hoje: 2026-10-28" in system_text(second.messages.requests[0])
    assert rows("SELECT status FROM runs WHERE id = %s", rid)[0]["status"] == "succeeded"


async def test_resume_refuses_a_different_model_or_prompt(store: RunStore) -> None:
    rid = run_id()
    with pytest.raises(anthropic.BadRequestError):
        await start(make_agent(FakeClient(bad_request()), Executor()), store, rid)
    record = await store.load(rid)
    assert (record.model, record.prompt_sha256) == (MODEL, "abc123")
    with pytest.raises(ValueError, match="prompt"):
        await resume_persisted(
            make_agent(FakeClient(), Executor()), store, record, prompt_sha256="changed"
        )
    other = make_agent(FakeClient(), Executor())
    other.config = AgentConfig(name="sales", model="claude-haiku-4-5", system="s")
    with pytest.raises(ValueError, match="refusing to resume on"):
        await resume_persisted(other, store, record, prompt_sha256="abc123")


async def test_max_steps_failure_can_be_resumed_with_a_fresh_budget(store: RunStore) -> None:
    rid = run_id()
    looping = [
        message(tool_use(f"toolu_{i}", "sales_by_period"), stop_reason="tool_use") for i in range(3)
    ]
    agent = make_agent(FakeClient(*looping), Executor())
    agent.config = AgentConfig(name="sales", model=MODEL, system="s", max_steps=3)
    with pytest.raises(MaxStepsExceeded) as info:
        await start(agent, store, rid)
    assert info.value.run_id == rid  # the caller knows what to resume
    resumed = make_agent(FakeClient(message(text("pronto"))), Executor())
    result = await resume_persisted(resumed, store, await store.load(rid), prompt_sha256="abc123")
    assert (result.text, result.steps, result.run_id) == ("pronto", 4, rid)


async def test_truncated_run_is_not_resumable(store: RunStore) -> None:
    rid = run_id()
    with pytest.raises(ResponseTruncated):
        await start(
            make_agent(
                FakeClient(message(text("Ontem voc"), stop_reason="max_tokens")), Executor()
            ),
            store,
            rid,
        )
    with pytest.raises(ValueError, match="truncated or refused"):
        await resume_persisted(
            make_agent(FakeClient(), Executor()),
            store,
            await store.load(rid),
            prompt_sha256="abc123",
        )


async def test_ask_sales_returns_its_run_id(store: RunStore) -> None:
    rid = run_id()
    result = await ask_sales(
        "oi",
        1,
        TODAY,
        client=FakeClient(message(text("ok"))),
        server=build_server(),
        run_id=rid,
        store=store,
    )
    assert result.run_id == rid
