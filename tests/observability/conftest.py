from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.otel_memory import memory_exporter


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = memory_exporter()
    exporter.clear()
    yield exporter
    exporter.clear()
