"""Approval routes: the owner decides on what the agents proposed, inside the key's scope."""

import hashlib
import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg.types.json import Jsonb

from brigada.api.main import app
from brigada.config import Settings, get_settings
from brigada.mcp_server.db import Conn, connect

pytestmark = pytest.mark.db

SETTINGS = Settings(api_keys="test-key=1,2; other-key=3", anthropic_api_key="sk-test")
KEY_ID = "key:" + hashlib.sha256(b"test-key").hexdigest()[:8]


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

    def make(
        restaurant_id: int, menu_item_id: int | None = None, payload: dict[str, Any] | None = None
    ) -> tuple[int, int]:
        kind = "pause_menu_item"
        if payload is not None:
            kind = "propose_purchase" if "ingredient_id" in payload else kind
            menu_item_id = -1
        elif menu_item_id is None:
            row = db.execute(
                "SELECT id FROM menu_items WHERE restaurant_id = %s AND active ORDER BY id LIMIT 1",
                (restaurant_id,),
            ).fetchone()
            assert row is not None
            menu_item_id = int(row["id"])
        row = db.execute(
            "INSERT INTO pending_actions (restaurant_id, kind, payload, reason, run_id) "
            "VALUES (%s, %s, %s, 'teste', %s) RETURNING id",
            (restaurant_id, kind, Jsonb(payload or {"menu_item_id": menu_item_id}), run_id),
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
    listed = client.get("/actions", params={"restaurant_id": 1}).json()
    assert mine in [a["action_id"] for a in listed]
    assert all(a["subject"] for a in listed), "the owner reads names, not ids"


def test_restaurant_filter_narrows_to_one_of_the_keys_restaurants(
    client: TestClient, propose: Any
) -> None:
    one, _ = propose(1)
    two, _ = propose(2)
    ids = [a["action_id"] for a in client.get("/actions", params={"restaurant_id": 1}).json()]
    assert one in ids and two not in ids
    assert two in [a["action_id"] for a in client.get("/actions").json()]


def test_status_filter(client: TestClient, propose: Any) -> None:
    pending, _ = propose(1)
    rejected, _ = propose(1)
    assert client.post(f"/actions/{rejected}/reject", json={"decided_by": "a"}).status_code == 200

    def ids(status: str | None) -> set[int]:
        params = {"restaurant_id": 1} | ({"status": status} if status else {})
        return {a["action_id"] for a in client.get("/actions", params=params).json()}

    assert pending in ids(None) and rejected not in ids(None)
    assert rejected in ids("rejected") and pending not in ids("rejected")
    assert {pending, rejected} <= ids("all")
    assert client.get("/actions", params={"status": "foo"}).status_code == 422


def test_purchases_show_the_ingredient_name_and_unit(
    client: TestClient, propose: Any, db: Conn
) -> None:
    ing = db.execute(
        "SELECT id, name, unit FROM ingredients WHERE restaurant_id = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    assert ing is not None
    action_id, _ = propose(1, payload={"ingredient_id": ing["id"], "qty": 5})
    listed = {a["action_id"]: a for a in client.get("/actions").json()}
    assert (listed[action_id]["subject"], listed[action_id]["unit"]) == (ing["name"], ing["unit"])


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
        "SELECT approved_by, error, args->>'principal' AS principal FROM tool_audit "
        "WHERE tool = 'human.approve' "
        "AND (args->>'action_id')::bigint = %s",
        (action_id,),
    ).fetchall()
    # The typed name is self-reported; the key that authenticated is recorded next to it.
    assert audit == [{"approved_by": "Rafael", "error": None, "principal": KEY_ID}]


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
    listed = {a["action_id"]: a for a in client.get("/actions").json()}
    assert listed[action_id]["subject"] is None, "R3's dish name must not show under R1"
    r = client.post(f"/actions/{action_id}/approve", json={"decided_by": "dono"})
    # The action exists, so it is a conflict, not "no such action".
    assert r.status_code == 409 and "target no longer exists" in r.json()["detail"]
    assert status_of(db, action_id) == "pending" and is_active(db, item)


@pytest.mark.parametrize("body", [{"decided_by": "  "}, {}, {"decided_by": "a", "x": 1}])
def test_decision_body_is_validated(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/actions/1/approve", json=body).status_code == 422


def test_names_resolve_inside_each_actions_own_restaurant(propose: Any, db: Conn) -> None:
    """A legit R3 action and an R1 action pointing at the same R3 dish, in one call."""
    from brigada.api.actions import _with_subjects
    from brigada.mcp_server import actions

    theirs, item = propose(3)
    tampered, _ = propose(1, item)
    found = {
        a.action_id: a
        for a in _with_subjects(actions.list_actions(restaurant_ids=frozenset({1, 3})))
    }
    assert found[theirs].subject is not None
    assert found[tampered].subject is None


def test_a_stored_payload_that_no_longer_validates_is_409_and_audited(
    client: TestClient, propose: Any, db: Conn
) -> None:
    action_id, _ = propose(1)
    db.execute(
        'UPDATE pending_actions SET payload = \'{"menu_item_id": "x"}\' WHERE id = %s',
        (action_id,),
    )
    r = client.post(f"/actions/{action_id}/approve", json={"decided_by": "dono"})
    assert r.status_code == 409
    assert r.json()["detail"] == "the stored action is invalid and was not applied"
    assert status_of(db, action_id) == "pending"
    row = db.execute(
        "SELECT error, restaurant_id FROM tool_audit WHERE tool = 'human.approve' "
        "AND (args->>'action_id')::bigint = %s",
        (action_id,),
    ).fetchone()
    assert row is not None and "ValidationError" in row["error"] and row["restaurant_id"] == 1


def test_a_probe_is_audited_under_the_probed_restaurant(
    client: TestClient, propose: Any, db: Conn
) -> None:
    action_id, _ = propose(3)
    assert client.post(f"/actions/{action_id}/approve", json={"decided_by": "x"}).status_code == 404
    row = db.execute(
        "SELECT error, restaurant_id, args->>'principal' AS principal FROM tool_audit "
        "WHERE tool = 'human.approve' AND (args->>'action_id')::bigint = %s",
        (action_id,),
    ).fetchone()
    # R3's owner can see that a key of another restaurant tried their action.
    assert row is not None and row["restaurant_id"] == 3 and row["principal"] == KEY_ID
    assert "OutOfScopeError" in row["error"]


def test_missing_and_out_of_scope_answer_the_same(
    client: TestClient, propose: Any, db: Conn
) -> None:
    theirs, _ = propose(3)
    missing = 2**31 - 1
    a = client.post(f"/actions/{theirs}/approve", json={"decided_by": "x"})
    try:
        b = client.post(f"/actions/{missing}/approve", json={"decided_by": "x"})
    finally:  # the fixture only cleans up the actions it created
        db.execute(
            "DELETE FROM tool_audit WHERE tool = 'human.approve' "
            "AND (args->>'action_id')::bigint = %s",
            (missing,),
        )
    assert a.status_code == b.status_code == 404
    assert a.json()["detail"].replace(str(theirs), "N") == b.json()["detail"].replace(
        str(missing), "N"
    )


def test_each_key_gets_its_own_principal(client: TestClient, propose: Any, db: Conn) -> None:
    action_id, _ = propose(3)
    r = client.post(
        f"/actions/{action_id}/reject", json={"decided_by": "d"}, headers={"X-API-Key": "other-key"}
    )
    assert r.status_code == 200
    row = db.execute(
        "SELECT args->>'principal' AS p FROM tool_audit WHERE tool = 'human.reject' "
        "AND (args->>'action_id')::bigint = %s",
        (action_id,),
    ).fetchone()
    assert row is not None and row["p"].startswith("key:") and row["p"] != KEY_ID


def test_two_concurrent_approvals_apply_once(client: TestClient, propose: Any, db: Conn) -> None:
    action_id, _ = propose(1)
    with ThreadPoolExecutor(2) as pool:
        codes = sorted(
            pool.map(
                lambda _: (
                    client.post(
                        f"/actions/{action_id}/approve", json={"decided_by": "a"}
                    ).status_code
                ),
                range(2),
            )
        )
    assert codes == [200, 409]
    ok = db.execute(
        "SELECT count(*) AS n FROM tool_audit WHERE tool = 'human.approve' AND error IS NULL "
        "AND (args->>'action_id')::bigint = %s",
        (action_id,),
    ).fetchone()
    assert ok is not None and ok["n"] == 1


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/actions?restaurant_id=0", None),
        ("post", "/actions/0/approve", {"decided_by": "a"}),
        ("post", "/actions/1/approve", {"decided_by": "a" * 101}),
        ("post", "/actions/1/approve", {"decided_by": "a", "note": "n" * 501}),
    ],
)
def test_bounds_are_validated(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    r = client.get(path) if method == "get" else client.post(path, json=body)
    assert r.status_code == 422
