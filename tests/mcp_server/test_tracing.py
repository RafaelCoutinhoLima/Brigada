"""Tracing in the MCP server: one span per ``tools/call``, wrapping checks, tool and audit."""

import runpy
from collections.abc import Sequence
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from brigada.mcp_server import audit
from brigada.mcp_server import server as server_module
from brigada.mcp_server.audit import restaurant_context, run_context
from brigada.mcp_server.db import Conn
from brigada.mcp_server.server import BrigadaMCPServer, build_server
from brigada.observability import tracing
from brigada.rag.embeddings import EmbeddingError
from tests.audit_cleanup import purge_audit

from .conftest import scoped_call


def tool_spans(spans: InMemorySpanExporter, tool: str) -> list[ReadableSpan]:
    return [s for s in spans.get_finished_spans() if s.name == f"execute_tool {tool}"]


@pytest.mark.db
async def test_a_tool_call_opens_a_span_with_the_tool_run_and_restaurant(
    server: BrigadaMCPServer, run_id: str, spans: InMemorySpanExporter
) -> None:
    await scoped_call(server, "list_menu_items", {"restaurant_id": 1})
    (span,) = tool_spans(spans, "list_menu_items")
    attrs = span.attributes or {}
    assert attrs["gen_ai.tool.name"] == "list_menu_items"
    assert attrs["brigada.run_id"] == run_id
    assert attrs["brigada.restaurant_id"] == 1
    assert "error.type" not in attrs
    assert span.status.status_code is not StatusCode.ERROR


@pytest.mark.db
async def test_a_rejected_call_is_an_error_span(
    server: BrigadaMCPServer, run_id: str, spans: InMemorySpanExporter
) -> None:
    with restaurant_context(1), pytest.raises(ToolError):
        await server.call_tool("list_menu_items", {"restaurant_id": 2})
    (span,) = tool_spans(spans, "list_menu_items")
    attrs = span.attributes or {}
    assert attrs["error.type"] == "ToolError"
    assert attrs["brigada.restaurant_id"] == 1  # the trusted scope, not the argument
    assert span.status.status_code is StatusCode.ERROR


@pytest.mark.db
async def test_an_unaudited_result_is_an_error_span(
    server: BrigadaMCPServer,
    run_id: str,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        audit, "record_standalone", lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
    )
    with pytest.raises(ToolError, match="could not be audited"):
        await scoped_call(server, "list_menu_items", {"restaurant_id": 1})
    (span,) = tool_spans(spans, "list_menu_items")
    assert (span.attributes or {})["error.type"] == "ToolError"


@pytest.mark.db
async def test_a_lone_surrogate_cannot_break_the_trace_export(
    server: BrigadaMCPServer, db: Conn, run_id: str, spans: InMemorySpanExporter
) -> None:
    """OTLP is protobuf: a span with invalid UTF-8 fails to encode and the whole batch, the
    probe and its neighbours, is dropped. Client-controlled text is escaped, like the audit."""
    try:
        with run_context(f"{run_id}\ud800"), pytest.raises(ToolError, match="invalid unicode"):
            await scoped_call(server, "evil\ud800tool", {"restaurant_id": 1})
    finally:
        purge_audit(db, "run_id = %s", (f"{run_id}\\ud800",))
    (span,) = tool_spans(spans, "evil\\ud800tool")
    assert (span.attributes or {})["brigada.run_id"] == f"{run_id}\\ud800"
    encode_spans([span]).SerializeToString()  # raises UnicodeEncodeError on a surrogate


class Unavailable:
    model = "unavailable"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("embedding service unavailable")


@pytest.mark.db
async def test_a_degraded_search_is_marked_on_the_tool_span(
    run_id: str, spans: InMemorySpanExporter
) -> None:
    args = {"restaurant_id": 2, "query": "frio", "limit": 3}
    result = await scoped_call(build_server(Unavailable()), "search_reviews", args)
    assert not result.is_error
    (span,) = tool_spans(spans, "search_reviews")
    assert (span.attributes or {})["brigada.search.degraded"] is True


def test_the_entrypoint_sets_up_tracing_before_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_setup(service_name: str = "", **kwargs: Any) -> None:
        calls.append(f"tracing {service_name}")

    monkeypatch.setattr(tracing, "setup_tracing", fake_setup)
    monkeypatch.setattr(server_module, "main", lambda: calls.append("serve"))
    runpy.run_module("brigada.mcp_server", run_name="__main__")
    assert calls == ["tracing brigada-mcp", "serve"]
