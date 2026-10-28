import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import brigada
from brigada.api.main import ORCHESTRATOR_MODULE, ORCHESTRATOR_PACKAGE, app, get_answerer
from brigada.config import Settings, get_settings
from brigada.runtime.types import AgentError, MaxStepsExceeded, ModelRefused, ResponseTruncated


@dataclass
class FakeOutcome:
    text: str
    cost_usd: float
    run_id: str | None = None


TEST_SETTINGS = Settings(api_keys="test-key=1,7; other-key=3")


@pytest.fixture
def client(spans: InMemorySpanExporter) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    with TestClient(app, headers={"X-API-Key": "test-key"}) as c:
        yield c
    app.dependency_overrides.clear()


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": brigada.__version__}
    assert len(resp.headers["X-Trace-ID"]) == 32
    assert resp.headers["X-Request-ID"]


def test_request_id_is_echoed(client: TestClient) -> None:
    resp = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["X-Request-ID"] == "abc-123"


def test_health_db_down_returns_503(client: TestClient) -> None:
    # Port 1 on loopback: connection refused immediately, no DNS involved.
    bad = Settings(database_url="postgresql://u:p@127.0.0.1:1/nope")
    app.dependency_overrides[get_settings] = lambda: bad
    resp = client.get("/health/db")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["status"] == "error"
    assert detail["error"] == "database unavailable"
    assert "127.0.0.1" not in resp.text


@pytest.mark.parametrize(
    "body",
    [
        {"question": "", "restaurant_id": 1},
        {"question": "   ", "restaurant_id": 1},
        {"question": "x" * 1001, "restaurant_id": 1},
        {"question": "ok", "restaurant_id": 0},
        {"question": "ok", "restaurant_id": -5},
        {"question": "ok"},
        {"restaurant_id": 1},
        {"question": "ok", "restaurant_id": 1, "extra": True},
    ],
)
def test_ask_validation_422(client: TestClient, body: dict[str, object]) -> None:
    app.dependency_overrides[get_answerer] = lambda: _fake_answerer
    assert client.post("/ask", json=body).status_code == 422


def test_ask_501_when_orchestrator_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty package + None for the submodule simulates "not written yet", regardless of
    # whether the real orchestrator exists.
    monkeypatch.setitem(sys.modules, ORCHESTRATOR_PACKAGE, types.ModuleType(ORCHESTRATOR_PACKAGE))
    monkeypatch.setitem(sys.modules, ORCHESTRATOR_MODULE, None)
    resp = client.post("/ask", json={"question": "Quanto vendi ontem?", "restaurant_id": 1})
    assert resp.status_code == 501
    assert "brigada.orchestration:answer" in resp.json()["detail"]


def test_get_answerer_finds_package_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = types.ModuleType(ORCHESTRATOR_PACKAGE)
    pkg.answer = _fake_answerer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, ORCHESTRATOR_PACKAGE, pkg)
    assert get_answerer() is _fake_answerer


def test_get_answerer_finds_submodule(monkeypatch: pytest.MonkeyPatch) -> None:
    sub = types.ModuleType(ORCHESTRATOR_MODULE)
    sub.answer = _fake_async_answerer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, ORCHESTRATOR_PACKAGE, types.ModuleType(ORCHESTRATOR_PACKAGE))
    monkeypatch.setitem(sys.modules, ORCHESTRATOR_MODULE, sub)
    assert get_answerer() is _fake_async_answerer


def test_ask_unrelated_import_error_is_not_masked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_import(name: str) -> object:
        raise ModuleNotFoundError("No module named 'some_dep'", name="some_dep")

    monkeypatch.setattr("brigada.api.main.importlib.import_module", broken_import)
    with pytest.raises(ModuleNotFoundError, match="some_dep"):
        client.post("/ask", json={"question": "oi", "restaurant_id": 1})


def _fake_answerer(question: str, restaurant_id: int, today: date) -> FakeOutcome:
    assert isinstance(today, date)
    return FakeOutcome(text=f"{restaurant_id}: {question}", cost_usd=0.0123, run_id="run-1")


async def _fake_async_answerer(question: str, restaurant_id: int, today: date) -> FakeOutcome:
    return _fake_answerer(question, restaurant_id, today)


@pytest.mark.parametrize("answerer", [_fake_answerer, _fake_async_answerer])
def test_ask_success(client: TestClient, spans: InMemorySpanExporter, answerer: object) -> None:
    app.dependency_overrides[get_answerer] = lambda: answerer
    resp = client.post("/ask", json={"question": "  Quanto vendi ontem?  ", "restaurant_id": 7})
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "7: Quanto vendi ontem?"
    assert data["run_id"] == "run-1"
    assert Decimal(data["cost_usd"]) == Decimal("0.0123")
    assert data["latency_ms"] >= 0
    assert data["trace_id"] == resp.headers["X-Trace-ID"]
    roots = [s for s in spans.get_finished_spans() if s.parent is None]
    assert len(roots) == 1
    assert roots[0].name == "POST /ask"
    assert format(roots[0].context.trace_id, "032x") == data["trace_id"]
    assert roots[0].attributes is not None
    assert roots[0].attributes["brigada.request_id"] == resp.headers["X-Request-ID"]


def test_answerer_errors_propagate(client: TestClient) -> None:
    def failing(question: str, restaurant_id: int, today: date) -> FakeOutcome:
        raise RuntimeError("LLM exploded")

    app.dependency_overrides[get_answerer] = lambda: failing
    with pytest.raises(RuntimeError, match="LLM exploded"):
        client.post("/ask", json={"question": "oi", "restaurant_id": 1})


def test_ask_without_run_id(client: TestClient) -> None:
    app.dependency_overrides[get_answerer] = lambda: (
        lambda q, r, t: FakeOutcome(text="ok", cost_usd=0)
    )
    resp = client.post("/ask", json={"question": "oi", "restaurant_id": 1})
    assert resp.status_code == 200
    assert resp.json()["run_id"] is None


def test_ask_requires_api_key(client: TestClient) -> None:
    app.dependency_overrides[get_answerer] = lambda: _fake_answerer
    body = {"question": "oi", "restaurant_id": 1}
    assert client.post("/ask", json=body, headers={"X-API-Key": ""}).status_code == 401
    assert client.post("/ask", json=body, headers={"X-API-Key": "wrong"}).status_code == 401


def test_ask_rejects_restaurant_outside_key_scope(client: TestClient) -> None:
    app.dependency_overrides[get_answerer] = lambda: _fake_answerer
    resp = client.post("/ask", json={"question": "oi", "restaurant_id": 3})
    assert resp.status_code == 403
    other = client.post(
        "/ask", json={"question": "oi", "restaurant_id": 3}, headers={"X-API-Key": "other-key"}
    )
    assert other.status_code == 200


def test_ask_rejects_everything_when_no_keys_configured(client: TestClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(api_keys="")
    app.dependency_overrides[get_answerer] = lambda: _fake_answerer
    assert client.post("/ask", json={"question": "oi", "restaurant_id": 1}).status_code == 401


def test_malformed_api_keys_setting_is_an_error() -> None:
    with pytest.raises(ValueError, match="Malformed"):
        Settings(api_keys="no-equals-sign").api_key_scopes()


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ModelRefused("cyber", "no"), 422),
        (ResponseTruncated("cut"), 502),
        (MaxStepsExceeded(10), 500),
    ],
)
def test_agent_errors_are_structured_and_traceable(
    client: TestClient, error: AgentError, status_code: int
) -> None:
    error.run_id = "run-42"

    def failing(question: str, restaurant_id: int, today: date) -> FakeOutcome:
        raise error

    app.dependency_overrides[get_answerer] = lambda: failing
    resp = client.post("/ask", json={"question": "oi", "restaurant_id": 1})
    assert resp.status_code == status_code
    body = resp.json()
    assert body["error_type"] == type(error).__name__
    assert body["run_id"] == "run-42"
    assert body["trace_id"] == resp.headers["X-Trace-ID"]
    assert resp.headers["X-Request-ID"]


def test_upstream_error_is_503_without_details(client: TestClient) -> None:
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def failing(question: str, restaurant_id: int, today: date) -> FakeOutcome:
        raise anthropic.APIConnectionError(message="secret-host:443 refused", request=request)

    app.dependency_overrides[get_answerer] = lambda: failing
    resp = client.post("/ask", json={"question": "oi", "restaurant_id": 1})
    assert resp.status_code == 503
    assert "secret-host" not in resp.text
