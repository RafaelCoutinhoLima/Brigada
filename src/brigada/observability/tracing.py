"""OpenTelemetry tracing setup and span helpers.

Attribute names follow the OpenTelemetry GenAI semantic conventions where one exists
(``gen_ai.*``); project-specific values live under ``brigada.*``.

Usage from the runtime::

    with agent_step_span("sales", kind="llm_call", model=model) as span:
        response = client.messages.create(...)
        record_usage(span, model, response.usage)

    with tool_span("get_sales") as span:
        result = await call_tool(...)

Both helpers also work as decorators on sync and async functions.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Literal, ParamSpec, TypeVar, cast, get_args

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from brigada.observability.cost import UsageLike, cost_from_usage

logger = logging.getLogger(__name__)

ExporterName = Literal["console", "otlp", "none"]
EXPORTER_ENV_VAR = "OTEL_EXPORTER"
DEFAULT_EXPORTER: ExporterName = "console"
TRACER_NAME = "brigada"

# Attribute keys (GenAI semantic conventions + project namespace).
ATTR_PROVIDER = "gen_ai.provider.name"
ATTR_OPERATION = "gen_ai.operation.name"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
ATTR_CACHE_CREATION_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_AGENT_NAME = "gen_ai.agent.name"
ATTR_ERROR_TYPE = "error.type"
ATTR_AGENT = "brigada.agent"
ATTR_STEP_KIND = "brigada.step.kind"
ATTR_COST_USD = "brigada.cost_usd"

_configured_provider: TracerProvider | None = None

P = ParamSpec("P")
R = TypeVar("R")


def resolve_exporter(value: str | None) -> ExporterName:
    """Validate an exporter name (``None`` or empty means the default)."""
    if value is None or value.strip() == "":
        return DEFAULT_EXPORTER
    name = value.strip().lower()
    allowed = get_args(ExporterName)
    if name not in allowed:
        raise ValueError(f"Invalid {EXPORTER_ENV_VAR}={value!r}; expected one of {allowed}")
    return cast(ExporterName, name)


def setup_tracing(
    service_name: str = "brigada", exporter: ExporterName | None = None
) -> TracerProvider:
    """Install an SDK ``TracerProvider`` as the global provider. Idempotent.

    ``exporter`` defaults to the ``OTEL_EXPORTER`` env var (``console`` if unset). ``none`` still
    installs an SDK provider (so trace ids are real) but exports nothing. ``otlp`` uses the
    HTTP/protobuf exporter, configured by the standard ``OTEL_EXPORTER_OTLP_*`` env vars.

    If an SDK provider is already installed (by an earlier call or by tests), it is returned
    unchanged: OpenTelemetry only allows setting the global provider once per process.
    """
    global _configured_provider
    name = resolve_exporter(exporter if exporter is not None else os.getenv(EXPORTER_ENV_VAR))

    if _configured_provider is not None:
        return _configured_provider
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        logger.info("SDK TracerProvider already installed; setup_tracing is a no-op")
        _configured_provider = current
        return current

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if name == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    elif name == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    trace.set_tracer_provider(provider)
    _configured_provider = provider
    logger.info("Tracing configured: service=%s exporter=%s", service_name, name)
    return provider


def get_tracer() -> trace.Tracer:
    # Resolved on every call so spans always go to the currently installed provider.
    return trace.get_tracer(TRACER_NAME)


def current_trace_id() -> str | None:
    """Hex trace id of the active span, or ``None`` if there is no valid span."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


def record_usage(span: Span, model: str, usage: UsageLike) -> Decimal:
    """Attach token counts and cost from an ``anthropic`` ``Usage`` to ``span``.

    Raises (never returns zero) for unknown models or unsupported pricing modifiers.
    """
    cost = cost_from_usage(model, usage)
    span.set_attribute(ATTR_INPUT_TOKENS, usage.input_tokens)
    span.set_attribute(ATTR_OUTPUT_TOKENS, usage.output_tokens)
    span.set_attribute(ATTR_CACHE_READ_TOKENS, usage.cache_read_input_tokens or 0)
    span.set_attribute(ATTR_CACHE_CREATION_TOKENS, usage.cache_creation_input_tokens or 0)
    # OTel attributes cannot hold Decimal; float is fine for display.
    span.set_attribute(ATTR_COST_USD, float(cost))
    return cost


@contextmanager
def _span(name: str, attributes: dict[str, AttributeValue]) -> Iterator[Span]:
    with get_tracer().start_as_current_span(
        name,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            span.set_attribute(ATTR_ERROR_TYPE, type(exc).__qualname__)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise


class SpanScope:
    """A span factory usable as a context manager or as a (sync/async) decorator.

    Each ``with`` block or decorated call opens a fresh span.
    """

    def __init__(self, name: str, attributes: dict[str, AttributeValue]) -> None:
        self._name = name
        self._attributes = attributes
        self._active: list[Any] = []

    def __enter__(self) -> Span:
        cm = _span(self._name, dict(self._attributes))
        span = cm.__enter__()
        self._active.append(cm)
        return span

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        cm = self._active.pop()
        return cm.__exit__(exc_type, exc, tb)

    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                with _span(self._name, dict(self._attributes)):
                    return await func(*args, **kwargs)

            return cast(Callable[P, R], async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with _span(self._name, dict(self._attributes)):
                return func(*args, **kwargs)

        return wrapper


def agent_step_span(
    agent: str,
    kind: str,
    model: str,
    attributes: dict[str, AttributeValue] | None = None,
) -> SpanScope:
    """Span for one agent step (e.g. kind="llm_call", "plan", "critique")."""
    attrs: dict[str, AttributeValue] = {
        ATTR_PROVIDER: "anthropic",
        ATTR_OPERATION: "chat",
        ATTR_REQUEST_MODEL: model,
        ATTR_AGENT_NAME: agent,
        ATTR_AGENT: agent,
        ATTR_STEP_KIND: kind,
    }
    attrs.update(attributes or {})
    return SpanScope(f"{kind} {agent}", attrs)


def tool_span(tool_name: str, attributes: dict[str, AttributeValue] | None = None) -> SpanScope:
    """Span for one tool execution."""
    attrs: dict[str, AttributeValue] = {
        ATTR_OPERATION: "execute_tool",
        ATTR_TOOL_NAME: tool_name,
    }
    attrs.update(attributes or {})
    return SpanScope(f"execute_tool {tool_name}", attrs)
