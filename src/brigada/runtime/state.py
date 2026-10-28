"""Persist agent runs so a run that died mid-way can be resumed.

After every model response and every batch of tool results, the whole conversation is saved
to ``runs.messages`` (a checkpoint) and the step is appended to ``steps`` (the history). A
resume loads the checkpoint and hands it to ``Agent.resume``.

Each write is its own short transaction on a fresh connection: simple, and a crash loses at
most the step in flight.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from anthropic.types import Message, MessageParam
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from brigada.runtime.agent import Agent
from brigada.runtime.types import RunResult, ToolCall, ToolOutcome


@dataclass(frozen=True)
class RunRecord:
    id: str
    agent: str
    restaurant_id: int
    question: str
    today: date
    status: str
    messages: list[MessageParam]
    result: RunResult


class RunStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    async def _execute(self, sql: str, params: Sequence[Any]) -> None:
        async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
            await conn.execute(sql, params)

    async def create(
        self,
        run_id: str,
        *,
        agent: str,
        restaurant_id: int,
        question: str,
        today: date,
        model: str,
        prompt_sha256: str,
        messages: Sequence[MessageParam],
    ) -> None:
        await self._execute(
            "INSERT INTO runs (id, agent, restaurant_id, question, today, model, prompt_sha256, "
            "messages) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                agent,
                restaurant_id,
                question,
                today,
                model,
                prompt_sha256,
                Jsonb(list(messages)),
            ),
        )

    async def checkpoint(
        self,
        run_id: str,
        *,
        step: int,
        kind: str,
        payload: Any,
        messages: Sequence[MessageParam],
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: Decimal = Decimal(0),
        latency_ms: int | None = None,
    ) -> None:
        """Append one step and move the run's checkpoint forward, atomically."""
        async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO steps (run_id, step, kind, payload, tokens_in, tokens_out, "
                    "cost_usd, latency_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        run_id,
                        step,
                        kind,
                        Jsonb(payload),
                        tokens_in,
                        tokens_out,
                        cost_usd,
                        latency_ms,
                    ),
                )
                cur = await conn.execute(
                    "UPDATE runs SET messages = %s, steps = %s, tokens_in = tokens_in + %s, "
                    "tokens_out = tokens_out + %s, cost_usd = cost_usd + %s "
                    "WHERE id = %s AND status = 'running'",
                    (Jsonb(list(messages)), step, tokens_in, tokens_out, cost_usd, run_id),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f"run {run_id!r} is not running; refusing to checkpoint")

    async def succeed(self, run_id: str, answer: str) -> None:
        await self._execute(
            "UPDATE runs SET status = 'succeeded', answer = %s, error = NULL, "
            "finished_at = now() WHERE id = %s",
            (answer, run_id),
        )

    async def fail(self, run_id: str, error: BaseException) -> None:
        await self._execute(
            "UPDATE runs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
            (f"{type(error).__name__}: {error}", run_id),
        )

    async def reopen(self, run_id: str) -> None:
        """Mark a failed run as running again before resuming it."""
        await self._execute(
            "UPDATE runs SET status = 'running', error = NULL, finished_at = NULL "
            "WHERE id = %s AND status = 'failed'",
            (run_id,),
        )

    async def load(self, run_id: str) -> RunRecord:
        async with await psycopg.AsyncConnection.connect(
            self.database_url, row_factory=dict_row
        ) as conn:
            cur = await conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,))
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"run {run_id!r} not found")
        messages: list[MessageParam] = row["messages"]
        result = RunResult(
            text=row["answer"] or "",
            steps=row["steps"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            cost_usd=Decimal(row["cost_usd"]),
            tools_called=_answered_tool_calls(messages),
        )
        return RunRecord(
            row["id"],
            row["agent"],
            row["restaurant_id"],
            row["question"],
            row["today"],
            row["status"],
            messages,
            result,
        )


class PersistingObserver:
    """``RunObserver`` that checkpoints every step into a ``RunStore``."""

    def __init__(self, store: RunStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id

    async def on_model_response(
        self,
        step: int,
        response: Message,
        cost_usd: Decimal,
        latency_ms: int,
        messages: Sequence[MessageParam],
    ) -> None:
        await self.store.checkpoint(
            self.run_id,
            step=step,
            kind="llm_call",
            payload={
                "stop_reason": response.stop_reason,
                "content": [b.to_dict() for b in response.content],
            },
            messages=messages,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )

    async def on_tool_results(
        self, step: int, outcomes: Sequence[ToolOutcome], messages: Sequence[MessageParam]
    ) -> None:
        await self.store.checkpoint(
            self.run_id,
            step=step,
            kind="tool_results",
            payload=[
                {"tool_use_id": o.tool_use_id, "content": o.content, "is_error": o.is_error}
                for o in outcomes
            ],
            messages=messages,
        )


async def run_persisted(
    agent: Agent,
    store: RunStore,
    run_id: str,
    *,
    restaurant_id: int,
    question: str,
    today: date,
    prompt_sha256: str,
) -> RunResult:
    """Run ``agent`` from scratch, saving every step under ``run_id``."""
    messages: list[MessageParam] = [{"role": "user", "content": question}]
    await store.create(
        run_id,
        agent=agent.config.name,
        restaurant_id=restaurant_id,
        question=question,
        today=today,
        model=agent.config.model,
        prompt_sha256=prompt_sha256,
        messages=messages,
    )
    return await _drive(agent, store, run_id, messages, RunResult(text="", steps=0))


async def resume_persisted(agent: Agent, store: RunStore, record: RunRecord) -> RunResult:
    """Continue a run that failed (crash, max steps, API outage) from its last checkpoint."""
    if record.status == "succeeded":
        raise ValueError(f"run {record.id!r} already succeeded")
    if record.status == "failed":
        await store.reopen(record.id)
    return await _drive(agent, store, record.id, record.messages, record.result)


async def _drive(
    agent: Agent, store: RunStore, run_id: str, messages: list[MessageParam], result: RunResult
) -> RunResult:
    agent.observer = PersistingObserver(store, run_id)
    try:
        result = await agent.resume(messages, result)
    except Exception as exc:  # CancelledError is not caught: the run stays "running", resumable
        await store.fail(run_id, exc)
        raise
    await store.succeed(run_id, result.text)
    return result


def _answered_tool_calls(messages: Sequence[MessageParam]) -> list[ToolCall]:
    """Tool calls that already have a result in the conversation (for ``tools_called``)."""
    answered: set[str] = set()
    calls: list[ToolCall] = []
    for m in messages:
        if isinstance(m["content"], str):
            continue
        for b in m["content"]:
            block: dict[str, Any] = b if isinstance(b, dict) else b.to_dict()  # type: ignore[union-attr]
            if block.get("type") == "tool_use":
                calls.append(ToolCall(block["id"], block["name"], dict(block["input"])))
            elif block.get("type") == "tool_result":
                answered.add(block["tool_use_id"])
    return [c for c in calls if c.id in answered]
