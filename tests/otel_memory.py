"""Process-wide in-memory span exporter for tests.

OpenTelemetry allows the global TracerProvider to be set only once per process, so every test
package that inspects spans shares this one.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_exporter: InMemorySpanExporter | None = None


def memory_exporter() -> InMemorySpanExporter:
    global _exporter
    if _exporter is None:
        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            provider = current
        else:
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
        _exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
    return _exporter
