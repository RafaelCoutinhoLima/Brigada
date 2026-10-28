"""The agent loop: call the model, run the tools it asks for, repeat until it answers.

One *step* is one model call. The conversation (``messages``) is the whole state: the API is
stateless, so every call resends it. Every way the loop can end without an answer raises a
named ``AgentError``.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import anthropic
from anthropic.types import Message, MessageParam, ToolParam

from brigada.observability.tracing import agent_step_span, record_usage, tool_span
from brigada.runtime.retry import DEFAULT_POLICY, RetryPolicy, with_retry
from brigada.runtime.tools import ToolExecutor, run_tools
from brigada.runtime.types import (
    MaxStepsExceeded,
    ModelRefused,
    ResponseTruncated,
    RunResult,
    ToolCall,
    ToolOutcome,
    UnexpectedStopReason,
)


class MessagesAPI(Protocol):
    async def create(self, **kwargs: Any) -> Message: ...


class ModelClient(Protocol):
    """The slice of ``anthropic.AsyncAnthropic`` the loop uses (lets tests pass a fake)."""

    @property
    def messages(self) -> MessagesAPI: ...


class RunObserver(Protocol):
    """Called after every change to the conversation, e.g. to persist it (see ``state.py``)."""

    async def on_model_response(
        self,
        step: int,
        response: Message,
        cost_usd: Decimal,
        latency_ms: int,
        messages: Sequence[MessageParam],
    ) -> None: ...

    async def on_tool_results(
        self, step: int, outcomes: Sequence[ToolOutcome], messages: Sequence[MessageParam]
    ) -> None: ...


class _NoObserver:
    async def on_model_response(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_tool_results(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class AgentConfig:
    name: str
    model: str
    system: str
    max_steps: int = 10
    max_tokens: int = 8000
    tool_timeout_seconds: float = 15.0
    retry: RetryPolicy = DEFAULT_POLICY


def make_client(api_key: str | None = None, timeout_seconds: float = 60.0) -> ModelClient:
    """Async client with SDK retries off: ``with_retry`` owns retrying (see retry.py)."""
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout_seconds)


@dataclass
class Agent:
    config: AgentConfig
    client: ModelClient
    tools: list[ToolParam]
    executor: ToolExecutor
    observer: RunObserver = field(default_factory=_NoObserver)

    async def run(self, question: str) -> RunResult:
        messages: list[MessageParam] = [{"role": "user", "content": question}]
        return await self.resume(messages, RunResult(text="", steps=0))

    async def resume(
        self, messages: list[MessageParam], result: RunResult, *, fresh_budget: bool = False
    ) -> RunResult:
        """Continue a conversation from wherever it stopped.

        If the last message is the model asking for tools (the process died before the tool
        results were saved), the tools run first. ``result`` carries the counters so far.
        With ``fresh_budget`` the run gets ``max_steps`` more model calls from here (used when
        resuming a run that failed, e.g. with ``MaxStepsExceeded``); otherwise the budget
        counts from the start of the conversation.
        """
        limit = result.steps + self.config.max_steps if fresh_budget else self.config.max_steps
        if not messages:
            raise ValueError("cannot resume an empty conversation")
        if _pending_tool_calls(messages):
            await self._run_tools(messages, result)
        elif messages[-1]["role"] == "assistant":
            # The model already answered; calling it again would send an assistant-last
            # conversation (a prefill), which current models reject.
            raise ValueError("conversation already ended with an assistant answer")
        while result.steps < limit:
            response = await self._call_model(messages, result)
            match response.stop_reason:
                case "end_turn" | "stop_sequence":
                    result.text = "".join(b.text for b in response.content if b.type == "text")
                    return result
                case "tool_use":
                    if not _pending_tool_calls(messages):
                        raise UnexpectedStopReason("tool_use without tool_use blocks")
                    await self._run_tools(messages, result)
                case "max_tokens":
                    raise ResponseTruncated(
                        f"response hit max_tokens={self.config.max_tokens} at step {result.steps}"
                    )
                case "refusal":
                    details = response.stop_details
                    raise ModelRefused(
                        getattr(details, "category", None), getattr(details, "explanation", None)
                    )
                case other:
                    raise UnexpectedStopReason(other)
        raise MaxStepsExceeded(self.config.max_steps)

    async def _call_model(self, messages: list[MessageParam], result: RunResult) -> Message:
        cfg = self.config
        step = result.steps + 1
        with agent_step_span(cfg.name, "llm_call", cfg.model, {"brigada.step": step}) as span:
            started = time.perf_counter()
            response = await with_retry(
                lambda: self.client.messages.create(
                    model=cfg.model,
                    max_tokens=cfg.max_tokens,
                    system=cfg.system,
                    tools=self.tools,
                    messages=messages,
                ),
                cfg.retry,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            cost = record_usage(span, cfg.model, response.usage)
            span.set_attribute("brigada.stop_reason", str(response.stop_reason))
        result.steps = step
        result.tokens_in += response.usage.input_tokens
        result.tokens_out += response.usage.output_tokens
        result.cost_usd += cost
        # The full content goes back, not just the text: tool_use blocks (and thinking blocks on
        # models that emit them) must be replayed unchanged on the next call.
        messages.append({"role": "assistant", "content": [b.to_dict() for b in response.content]})
        await self.observer.on_model_response(step, response, cost, latency_ms, messages)
        return response

    async def _run_tools(self, messages: list[MessageParam], result: RunResult) -> None:
        calls = _pending_tool_calls(messages)
        result.tools_called.extend(calls)
        outcomes = await run_tools(calls, self._traced_executor, self.config.tool_timeout_seconds)
        # All results in ONE user message: splitting them teaches the model to stop calling
        # tools in parallel.
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": o.tool_use_id,
                        "content": o.content,
                        "is_error": o.is_error,
                    }
                    for o in outcomes
                ],
            }
        )
        await self.observer.on_tool_results(result.steps, outcomes, messages)

    async def _traced_executor(self, call: ToolCall) -> str:
        with tool_span(call.name, {"brigada.tool_use_id": call.id}):
            return await self.executor(call)


def _pending_tool_calls(messages: Sequence[MessageParam]) -> list[ToolCall]:
    """Tool calls in the last message if it is the assistant's (i.e. not yet answered)."""
    if not messages or messages[-1]["role"] != "assistant":
        return []
    content = messages[-1]["content"]
    if isinstance(content, str):
        return []
    calls = []
    for block in content:
        b = block if isinstance(block, dict) else block.to_dict()  # type: ignore[union-attr]
        if b.get("type") == "tool_use":
            calls.append(ToolCall(id=b["id"], name=b["name"], input=dict(b["input"])))
    return calls
