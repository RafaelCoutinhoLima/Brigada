"""Approval routes: the owner decides on what the agents proposed, inside the key's scope."""

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg.types.json import Jsonb

from brigada.api.main import app
from brigada.config import Settings, get_settings
from brigada.mcp_server.db import Conn, connect

pytestmark = pytest.mark.db

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


@pytest.fixture
def db() -> Iterator[Conn]:
    with connect() as conn:
        conn.autocommit = True
        yield conn


@pytest.fixture
def propose(db: Conn) -> Iterator[Any]:
    """Create pause actions tagged with a test run id; remove them and their effects after."""
    run_id = f"test-{uuid.uuid4()}"
    snapshot = db.execute("SELECT id, active FROM menu_items").fetchall()

    def make(restaurant_id: int, menu_item_id: int | None = None) -> tuple[int, int]:
        if menu_item_id is None:
            row = db.execute(
                "SELECT id FROM menu_items WHERE restaurant_id = %s AND active ORDER BY id LIMIT 1",
                (restaurant_id,),
            ).fetchone()
            assert row is not None
            menu_item_id = int(row["id"])
        row = db.execute(
            "INSERT INTO pending_actions (restaurant_id, kind, payload, reason, run_id) "
            "VALUES (%s, 'pause_menu_item', %s, 'teste', %s) RETURNING id",
            (restaurant_id, Jsonb({"menu_item_id": menu_item_id}), run_id),
        ).fetchone()
        assert row is not None
        return int(row["id"]), menu_item_id

    yield make
    ids = [
        r["id"]
        for r in db.execute(
            "SELECT id FROM pending_actions WHERE run_id = %s", (run_id,)
        ).fetchall()
    ]
    with db.transaction():
        db.execute(
            "DELETE FROM tool_audit WHERE tool LIKE 'human.%%' "
            "AND (args->>'action_id')::bigint = ANY(%s)",
            (ids,),
        )
        db.execute("DELETE FROM pending_actions WHERE id = ANY(%s)", (ids,))
        for row in snapshot:
            db.execute(
                "UPDATE menu_items SET active = %s WHERE id = %s AND active IS DISTINCT FROM %s",
                (row["active"], row["id"], row["active"]),
            )


def is_active(db: Conn, item_id: int) -> bool:
    row = db.execute("SELECT active FROM menu_items WHERE id = %s", (item_id,)).fetchone()
    assert row is not None
    return bool(row["active"])


def status_of(db: Conn, action_id: int) -> str:
    row = db.execute("SELECT status FROM pending_actions WHERE id = %s", (action_id,)).fetchone()
    assert row is not None
    return str(row["status"])


def test_list_shows_only_the_keys_restaurants(client: TestClient, propose: Any) -> None:
    mine, _ = propose(1)
    theirs, _ = propose(3)
    ids = [a["action_id"] for a in client.get("/actions").json()]
    assert mine in ids and theirs not in ids
    ids = [a["action_id"] for a in client.get("/actions", params={"restaurant_id": 1}).json()]
    assert mine in ids


def test_list_rejects_a_restaurant_outside_the_scope(client: TestClient) -> None:
    assert client.get("/actions", params={"restaurant_id": 3}).status_code == 403


def test_routes_require_a_key(client: TestClient) -> None:
    assert client.get("/actions", headers={"X-API-Key": ""}).status_code == 401
    r = client.post("/actions/1/approve", json={"decided_by": "x"}, headers={"X-API-Key": "no"})
    assert r.status_code == 401


def test_approve_applies_the_change_and_records_who(
    client: TestClient, propose: Any, db: Conn
) -> None:
    action_id, item = propose(1)
    r = client.post(f"/actions/{action_id}/approve", json={"decided_by": "Rafael"})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["decided_by"]) == ("executed", "Rafael")
    assert not is_active(db, item)
    audit = db.execute(
        "SELECT approved_by, error FROM tool_audit WHERE tool = 'human.approve' "
        "AND (args->>'action_id')::bigint = %s",
        (action_id,),
    ).fetchall()
    assert audit == [{"approved_by": "Rafael", "error": None}]


def test_another_restaurants_action_is_404_and_untouched(
    client: TestClient, propose: Any, db: Conn
) -> None:
    action_id, item = propose(3)
    for verb in ("approve", "reject"):
        r = client.post(f"/actions/{action_id}/{verb}", json={"decided_by": "intruso"})
        assert r.status_code == 404
        assert r.json()["detail"] == f"pending action {action_id} does not exist"
    assert status_of(db, action_id) == "pending" and is_active(db, item)
    # The right key can decide it.
    r = client.post(
        f"/actions/{action_id}/reject",
        json={"decided_by": "dono"},
        headers={"X-API-Key": "other-key"},
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected"


def test_deciding_twice_is_409(client: TestClient, propose: Any) -> None:
    action_id, _ = propose(1)
    assert client.post(f"/actions/{action_id}/reject", json={"decided_by": "a"}).status_code == 200
    r = client.post(f"/actions/{action_id}/approve", json={"decided_by": "a"})
    assert r.status_code == 409 and "already rejected" in r.json()["detail"]


def test_reject_does_not_apply(client: TestClient, propose: Any, db: Conn) -> None:
    action_id, item = propose(1)
    r = client.post(f"/actions/{action_id}/reject", json={"decided_by": "dono", "note": "não"})
    assert r.status_code == 200 and r.json()["decision_note"] == "não"
    assert is_active(db, item)


def test_a_tampered_action_for_another_tenant_is_not_applied(
    client: TestClient, propose: Any, db: Conn
) -> None:
    other = db.execute(
        "SELECT id FROM menu_items WHERE restaurant_id = 3 AND active ORDER BY id LIMIT 1"
    ).fetchone()
    assert other is not None
    action_id, item = propose(1, int(other["id"]))  # R1 action pointing at R3's item
    r = client.post(f"/actions/{action_id}/approve", json={"decided_by": "dono"})
    assert r.status_code == 404
    assert status_of(db, action_id) == "pending" and is_active(db, item)


@pytest.mark.parametrize("body", [{"decided_by": "  "}, {}, {"decided_by": "a", "x": 1}])
def test_decision_body_is_validated(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/actions/1/approve", json=body).status_code == 422
