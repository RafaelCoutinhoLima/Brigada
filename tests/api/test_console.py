import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from brigada.api import console
from brigada.api.main import app
from brigada.config import Settings, get_settings
from brigada.mcp_server.server import build_server
from brigada.orchestration.store import OrchestrationStore
from brigada.runtime.specialists import SALES, ask
from brigada.runtime.state import RunStore
from tests.runtime.fakes import FakeClient, message, text, tool_use

TODAY = date(2026, 10, 28)
SETTINGS = Settings(api_keys="test-key=1,7; other-key=3", anthropic_api_key="sk-test")


@pytest.fixture
def client(spans: InMemorySpanExporter) -> Iterator[TestClient]:
    settings = SETTINGS.model_copy(
        update={"database_url": os.environ.get("DATABASE_URL", SETTINGS.database_url)}
    )
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, headers={"X-API-Key": "test-key"}) as c:
        yield c
    app.dependency_overrides.clear()


def test_page_is_served_without_a_key(client: TestClient) -> None:
    resp = client.get("/console", headers={"X-API-Key": ""})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "BRIGADA" in resp.text
    assert "innerHTML = `" not in resp.text.split("render(state)")[1]  # data goes via textContent


def test_runs_need_a_valid_key(client: TestClient) -> None:
    resp = client.get("/console/runs?restaurant_id=1", headers={"X-API-Key": "nope"})
    assert resp.status_code == 401


def test_runs_refuse_a_restaurant_outside_the_key_scope(client: TestClient) -> None:
    assert client.get("/console/runs?restaurant_id=3").status_code == 403


def test_dispatch_refuses_a_restaurant_outside_the_key_scope(client: TestClient) -> None:
    body = {"question": "oi", "restaurant_id": 3, "agents": ["sales"]}
    assert client.post("/console/dispatch", json=body).status_code == 403


def test_dispatch_rejects_unknown_agents(client: TestClient) -> None:
    body = {"question": "oi", "restaurant_id": 1, "agents": ["chef"]}
    assert client.post("/console/dispatch", json=body).status_code == 422


def test_dispatch_without_model_key_is_503(client: TestClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(api_keys="test-key=1")
    body = {"question": "oi", "restaurant_id": 1, "agents": ["sales"]}
    resp = client.post("/console/dispatch", json=body)
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_dispatch_starts_one_run_per_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, str, int, date, str]] = []

    async def fake_ask(specialist: Any, question: str, rid: int, today: date, **kw: Any) -> None:
        started.append((specialist.name, question, rid, today, kw["run_id"]))
        await asyncio.sleep(0)

    monkeypatch.setattr(console.specialists, "ask", fake_ask)
    body = {
        "question": "Por que caiu?",
        "restaurant_id": 1,
        "agents": ["sales", "operations", "sales"],
        "today": "2026-10-28",
    }
    resp = client.post("/console/dispatch", json=body)
    assert resp.status_code == 202
    runs = resp.json()["runs"]
    assert [r["agent"] for r in runs] == ["sales", "operations"]
    client.get("/health")  # let the background tasks run on the app's loop
    assert sorted((s[0], s[4]) for s in started) == sorted((r["agent"], r["run_id"]) for r in runs)
    assert {s[3] for s in started} == {TODAY}


@pytest.mark.db
async def test_runs_show_each_step_and_hide_error_details(client: TestClient) -> None:
    store = RunStore(os.environ["DATABASE_URL"])
    ok, bad = f"test-{uuid.uuid4().hex}", f"test-{uuid.uuid4().hex}"
    args = {"restaurant_id": 1, "start_date": "2026-10-27", "end_date": "2026-10-27"}
    model = FakeClient(
        message(tool_use("toolu_1", "sales_by_period", **args), stop_reason="tool_use"),
        message(text("Ontem: R$ 10,00.")),
    )
    try:
        await ask(
            SALES,
            "Quanto vendi ontem?",
            1,
            TODAY,
            client=model,
            server=build_server(),
            run_id=ok,
            store=store,
        )
        secret = anthropic.BadRequestError(
            "secret upstream detail",
            response=httpx2.Response(
                400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            body=None,
        )
        with pytest.raises(anthropic.BadRequestError):
            await ask(
                SALES,
                "oi",
                1,
                TODAY,
                client=FakeClient(secret),
                server=build_server(),
                run_id=bad,
                store=store,
            )

        resp = client.get("/console/runs?restaurant_id=1&limit=30")
        assert resp.status_code == 200
        state = resp.json()
        assert {a["name"] for a in state["agents"]} == {"sales", "menu", "operations", "inventory"}
        runs = {r["id"]: r for r in state["runs"]}

        good = runs[ok]
        assert good["status"] == "succeeded"
        assert good["answer"] == "Ontem: R$ 10,00."
        kinds = [(e["kind"], e["tool"]) for e in good["events"]]
        assert kinds == [
            ("call", "sales_by_period"),
            ("result", "sales_by_period"),
            ("text", None),
        ]
        assert "start_date=2026-10-27" in good["events"][0]["text"]
        assert good["events"][1]["size"] > 0 and not good["events"][1]["is_error"]

        failed = runs[bad]
        assert failed["status"] == "failed"
        assert failed["error_type"] == "BadRequestError"
        assert "secret" not in resp.text
    finally:
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            conn.execute("DELETE FROM runs WHERE id = ANY(%s)", ([ok, bad],))


def test_dispatch_through_the_orchestrator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[dict[str, Any]] = []

    async def fake_orchestrate(question: str, rid: int, today: date, **kw: Any) -> None:
        started.append({"question": question, "rid": rid, "today": today, **kw})

    monkeypatch.setattr(console, "orchestrate", fake_orchestrate)
    body = {
        "question": "Por que caiu?",
        "restaurant_id": 1,
        "orchestrate": True,
        "today": "2026-10-28",
    }
    resp = client.post("/console/dispatch", json=body)
    assert resp.status_code == 202
    out = resp.json()
    assert out["runs"] == [] and len(out["orchestration_id"]) == 32
    client.get("/health")
    assert started[0]["orchestration_id"] == out["orchestration_id"]
    assert (started[0]["rid"], started[0]["today"]) == (1, TODAY)


@pytest.mark.db
async def test_runs_show_the_latest_orchestration_with_step_status(client: TestClient) -> None:
    url = os.environ["DATABASE_URL"]
    oid = f"test-{uuid.uuid4().hex}"
    store = OrchestrationStore(url)
    await store.create(oid, restaurant_id=1, question="Por que caiu?", today=TODAY)
    plan = {
        "steps": [
            {"id": "s1", "specialist": "sales", "task": "vendas", "depends_on": []},
            {"id": "s2", "specialist": "menu", "task": "cardápio", "depends_on": ["s1"]},
        ],
        "direct_answer": None,
    }
    reviews = [
        {
            "approved": False,
            "issues": ["falta X"],
            "extra_steps": [
                {"id": "s3", "specialist": "operations", "task": "entregas", "depends_on": []}
            ],
        }
    ]
    await store.update(oid, plan=plan, review=reviews, rounds=1)
    runs = RunStore(url)
    for rid, agent, step in [
        ("a", "planner", "plan"),
        ("b", "sales", "s1"),
        ("c", "critic", "review-1"),
    ]:
        await runs.create(
            f"{oid}-{rid}",
            agent=agent,
            restaurant_id=1,
            question="q",
            today=TODAY,
            model="m",
            prompt_sha256="x",
            messages=[],
            orchestration_id=oid,
            plan_step=step,
        )
        await runs.succeed(f"{oid}-{rid}", "ok")
    try:
        state = client.get("/console/runs?restaurant_id=1").json()
        o = state["orchestrations"][0]
        assert o["id"] == oid and o["status"] == "running"
        assert [(s["id"], s["status"], s["extra"]) for s in o["steps"]] == [
            ("s1", "succeeded", False),
            ("s2", "pending", False),
            ("s3", "pending", True),
        ]
        assert [r["plan_step"] for r in o["roles"]] == ["plan", "review-1"]
        assert o["reviews"][0]["issues"] == ["falta X"]
        await store.fail(
            oid, RuntimeError("secret detail"), tokens_in=1, tokens_out=1, cost_usd=Decimal(0)
        )
        o = client.get("/console/runs?restaurant_id=1").json()["orchestrations"][0]
        assert o["error_type"] == "RuntimeError"
        assert [s["status"] for s in o["steps"]] == ["succeeded", "skipped", "skipped"]
        assert "secret" not in str(o)
    finally:
        with psycopg.connect(url) as conn:
            conn.execute("DELETE FROM orchestrations WHERE id = %s", (oid,))


def test_dispatch_is_capped_per_restaurant_and_globally(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()

    async def slow_ask(*args: Any, **kw: Any) -> None:
        await gate.wait()

    monkeypatch.setattr(console.specialists, "ask", slow_ask)
    monkeypatch.setattr(console, "MAX_IN_FLIGHT_PER_RESTAURANT", 3)
    body = {"question": "oi", "restaurant_id": 1, "agents": ["sales", "menu", "operations"]}
    try:
        assert client.post("/console/dispatch", json=body).status_code == 202
        refused = client.post("/console/dispatch", json={**body, "agents": ["sales"]})
        assert refused.status_code == 429 and refused.headers["Retry-After"]
        monkeypatch.setattr(console, "MAX_IN_FLIGHT", 3)
        other = client.post("/console/dispatch", json={**body, "restaurant_id": 7})
        assert other.status_code == 429  # the global cap protects every tenant
    finally:
        gate.set()
        client.get("/health")  # let the tasks finish
    assert sum(w for _, w in console._tasks.values()) == 0


@pytest.mark.db
async def test_runs_never_show_another_restaurants_data(client: TestClient) -> None:
    url = os.environ["DATABASE_URL"]
    runs, store = RunStore(url), OrchestrationStore(url)
    rid, oid = f"test-{uuid.uuid4().hex}", f"test-{uuid.uuid4().hex}"
    await runs.create(
        rid,
        agent="sales",
        restaurant_id=2,
        question="segredo do 2",
        today=TODAY,
        model="m",
        prompt_sha256="x",
        messages=[],
    )
    await store.create(oid, restaurant_id=2, question="segredo do 2", today=TODAY)
    try:
        state = client.get("/console/runs?restaurant_id=1").json()
        assert rid not in str(state) and oid not in str(state)
        assert "segredo do 2" not in str(state)
    finally:
        with psycopg.connect(url) as conn:
            conn.execute("DELETE FROM runs WHERE id = %s", (rid,))
            conn.execute("DELETE FROM orchestrations WHERE id = %s", (oid,))


@pytest.mark.db
async def test_extra_steps_of_the_last_review_are_not_shown(client: TestClient) -> None:
    url = os.environ["DATABASE_URL"]
    oid = f"test-{uuid.uuid4().hex}"
    store = OrchestrationStore(url)
    await store.create(oid, restaurant_id=1, question="q", today=TODAY)
    step = {"specialist": "sales", "task": "t", "depends_on": []}
    reviews = [
        {"approved": False, "issues": ["a"], "extra_steps": [{"id": "s2", **step}]},
        {"approved": False, "issues": ["b"], "extra_steps": [{"id": "s3", **step}]},
    ]
    await store.update(oid, plan={"steps": [{"id": "s1", **step}]}, review=reviews, rounds=1)
    try:
        o = client.get("/console/runs?restaurant_id=1").json()["orchestrations"][0]
        assert [s["id"] for s in o["steps"]] == ["s1", "s2"]
    finally:
        with psycopg.connect(url) as conn:
            conn.execute("DELETE FROM orchestrations WHERE id = %s", (oid,))


def _local(settings: Settings, host: str = "127.0.0.1") -> TestClient:
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, client=(host, 50000), base_url="http://127.0.0.1:8000")


def test_local_session_is_off_by_default() -> None:
    with _local(SETTINGS) as c:
        assert c.get("/console/local-session").status_code == 404
    app.dependency_overrides.clear()


def test_local_session_hands_the_key_only_to_loopback_without_proxy() -> None:
    on = SETTINGS.model_copy(update={"console_local_key": "test-key"})
    with _local(on) as c:
        assert c.get("/console/local-session").json() == {"key": "test-key"}
        proxied = c.get("/console/local-session", headers={"X-Forwarded-For": "203.0.113.9"})
        assert proxied.status_code == 404  # a reverse proxy makes everyone look local
    with _local(on, host="192.168.0.20") as c:
        assert c.get("/console/local-session").status_code == 404
    app.dependency_overrides.clear()


def test_local_session_refuses_a_key_that_is_not_a_real_api_key() -> None:
    with _local(SETTINGS.model_copy(update={"console_local_key": "made-up"})) as c:
        assert c.get("/console/local-session").status_code == 404
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example:8000"},  # DNS rebinding: hostile name resolving to 127.0.0.1
        {"Sec-Fetch-Site": "cross-site"},
        {"X-Forwarded-Host": "brigada.example"},
        {"Via": "1.1 proxy"},
        {"CF-Connecting-IP": "203.0.113.9"},
    ],
)
def test_local_session_refuses_rebinding_cross_site_and_any_proxy(headers: dict[str, str]) -> None:
    with _local(SETTINGS.model_copy(update={"console_local_key": "test-key"})) as c:
        assert c.get("/console/local-session", headers=headers).status_code == 404
    app.dependency_overrides.clear()
