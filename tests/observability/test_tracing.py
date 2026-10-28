import asyncio

import pytest
from anthropic.types import Usage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from brigada.observability.cost import UnknownModelError, cost_usd
from brigada.observability.tracing import (
    agent_step_span,
    current_trace_id,
    record_usage,
    resolve_exporter,
    setup_tracing,
    tool_span,
)


class Boom(Exception):
    pass


def test_agent_step_span_attributes(spans: InMemorySpanExporter) -> None:
    with agent_step_span("sales", kind="llm_call", model="claude-sonnet-4-6") as span:
        cost = record_usage(
            span,
            "claude-sonnet-4-6",
            Usage(input_tokens=1000, output_tokens=500, cache_read_input_tokens=100),
        )

    (finished,) = spans.get_finished_spans()
    attrs = dict(finished.attributes or {})
    assert finished.name == "llm_call sales"
    assert attrs["gen_ai.request.model"] == "claude-sonnet-4-6"
    assert attrs["gen_ai.provider.name"] == "anthropic"
    assert attrs["brigada.agent"] == "sales"
    assert attrs["brigada.step.kind"] == "llm_call"
    assert attrs["gen_ai.usage.input_tokens"] == 1000
    assert attrs["gen_ai.usage.output_tokens"] == 500
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 100
    assert attrs["brigada.cost_usd"] == pytest.approx(float(cost))
    assert cost == cost_usd("claude-sonnet-4-6", 1000, 500, cache_read_input_tokens=100)
    assert finished.status.status_code is StatusCode.UNSET


def test_tool_span_attributes_and_nesting(spans: InMemorySpanExporter) -> None:
    with agent_step_span("sales", kind="llm_call", model="claude-haiku-4-5"):
        with tool_span("get_sales", attributes={"brigada.restaurant_id": 3}):
            pass

    tool, step = spans.get_finished_spans()
    assert tool.name == "execute_tool get_sales"
    assert tool.attributes is not None
    assert tool.attributes["gen_ai.tool.name"] == "get_sales"
    assert tool.attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool.attributes["brigada.restaurant_id"] == 3
    assert tool.parent is not None
    assert tool.parent.span_id == step.context.span_id


def test_exception_recorded_and_reraised(spans: InMemorySpanExporter) -> None:
    with pytest.raises(Boom, match="db down"):
        with tool_span("get_sales"):
            raise Boom("db down")

    (finished,) = spans.get_finished_spans()
    assert finished.status.status_code is StatusCode.ERROR
    assert "db down" in (finished.status.description or "")
    assert finished.attributes is not None
    assert finished.attributes["error.type"] == "Boom"
    exception_events = [e for e in finished.events if e.name == "exception"]
    assert len(exception_events) == 1
    assert exception_events[0].attributes is not None
    assert exception_events[0].attributes["exception.type"].endswith("Boom")


def test_record_usage_unknown_model_raises(spans: InMemorySpanExporter) -> None:
    with pytest.raises(UnknownModelError):
        with agent_step_span("sales", kind="llm_call", model="nope") as span:
            record_usage(span, "nope", Usage(input_tokens=1, output_tokens=1))
    (finished,) = spans.get_finished_spans()
    assert finished.status.status_code is StatusCode.ERROR


def test_sync_decorator(spans: InMemorySpanExporter) -> None:
    @tool_span("add")
    def add(a: int, b: int) -> int:
        return a + b

    assert add(1, 2) == 3
    assert add(3, 4) == 7
    finished = spans.get_finished_spans()
    assert [s.name for s in finished] == ["execute_tool add", "execute_tool add"]
    assert finished[0].context.span_id != finished[1].context.span_id


def test_async_decorator_records_errors(spans: InMemorySpanExporter) -> None:
    @agent_step_span("planner", kind="plan", model="claude-opus-5-5")
    async def plan(fail: bool) -> str:
        await asyncio.sleep(0)
        if fail:
            raise Boom("bad plan")
        return "ok"

    assert asyncio.run(plan(False)) == "ok"
    with pytest.raises(Boom):
        asyncio.run(plan(True))

    ok, failed = spans.get_finished_spans()
    assert ok.status.status_code is StatusCode.UNSET
    assert failed.status.status_code is StatusCode.ERROR
    assert len([e for e in failed.events if e.name == "exception"]) == 1


def test_current_trace_id(spans: InMemorySpanExporter) -> None:
    with tool_span("x") as span:
        trace_id = current_trace_id()
        assert trace_id == format(span.get_span_context().trace_id, "032x")
        assert trace_id is not None and len(trace_id) == 32


def test_setup_tracing_is_idempotent(spans: InMemorySpanExporter) -> None:
    first = setup_tracing(exporter="none")
    second = setup_tracing(exporter="console")
    assert first is second


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "console"),
        ("", "console"),
        ("OTLP", "otlp"),
        (" none ", "none"),
        ("console", "console"),
    ],
)
def test_resolve_exporter(value: str | None, expected: str) -> None:
    assert resolve_exporter(value) == expected


def test_resolve_exporter_invalid() -> None:
    with pytest.raises(ValueError, match="OTEL_EXPORTER"):
        resolve_exporter("jaeger")


def test_setup_tracing_rejects_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER", "zipkin")
    with pytest.raises(ValueError):
        setup_tracing()
