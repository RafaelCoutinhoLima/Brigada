"""Purchase order routes: list what is on the way, receive it, inside the key's scope."""

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
from brigada.mcp_server.db import connect
from tests.audit_cleanup import purge_audit

pytestmark = pytest.mark.db

SETTINGS = Settings(api_keys="test-key=1; other-key=3", anthropic_api_key="sk-test")


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
def order(client: TestClient) -> Iterator[int]:
    """An approved purchase of 5 units of restaurant 1's first ingredient."""
    run_id = f"test-{uuid.uuid4()}"
    with connect() as db:
        db.autocommit = True
        ing = db.execute(
            "SELECT id FROM ingredients WHERE restaurant_id = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        assert ing is not None
        row = db.execute(
            "INSERT INTO pending_actions (restaurant_id, kind, payload, reason, run_id) "
            "VALUES (1, 'propose_purchase', %s, 'teste', %s) RETURNING id",
            (Jsonb({"ingredient_id": ing["id"], "qty": 5}), run_id),
        ).fetchone()
        assert row is not None
        action_id = row["id"]
        r = client.post(f"/actions/{action_id}/approve", json={"decided_by": "dono"})
        assert r.status_code == 200, r.text
        po = db.execute(
            "SELECT id FROM purchase_orders WHERE source_action_id = %s", (action_id,)
        ).fetchone()
        assert po is not None
        yield int(po["id"])
        with db.transaction():
            gone = db.execute(
                "DELETE FROM purchase_orders WHERE id = %s RETURNING movement_id", (po["id"],)
            ).fetchone()
            if gone and gone["movement_id"]:
                db.execute("DELETE FROM stock_movements WHERE id = %s", (gone["movement_id"],))
            purge_audit(
                db,
                "(tool LIKE 'human.%%' AND (args->>'action_id')::bigint = %s) OR "
                "(tool = 'human.receive_purchase' "
                "AND (args->>'purchase_order_id')::bigint = %s)",
                (action_id, po["id"]),
            )
            db.execute("DELETE FROM pending_actions WHERE id = %s", (action_id,))


def test_open_orders_are_listed_for_the_keys_restaurants(client: TestClient, order: int) -> None:
    mine = client.get("/purchase-orders", params={"restaurant_id": 1}).json()
    assert [o["purchase_order_id"] for o in mine] == [order]
    assert mine[0]["status"] == "ordered" and mine[0]["ingredient"]
    theirs = client.get("/purchase-orders", headers={"X-API-Key": "other-key"}).json()
    assert order not in [o["purchase_order_id"] for o in theirs]
    assert client.get("/purchase-orders", params={"restaurant_id": 3}).status_code == 403


def test_receive_then_it_is_no_longer_open(client: TestClient, order: int) -> None:
    body = {"received_by": "estoquista", "unit_cost": "12.50"}
    r = client.post(f"/purchase-orders/{order}/receive", json=body)
    assert r.status_code == 200 and r.json()["status"] == "received"
    assert client.post(f"/purchase-orders/{order}/receive", json=body).status_code == 409
    assert client.get("/purchase-orders").json() == []
    done = client.get("/purchase-orders", params={"status": "received"}).json()
    assert [o["unit_cost"] for o in done if o["purchase_order_id"] == order] == [12.5]


def test_another_restaurant_gets_404(client: TestClient, order: int) -> None:
    r = client.post(
        f"/purchase-orders/{order}/receive",
        json={"received_by": "x", "unit_cost": "1"},
        headers={"X-API-Key": "other-key"},
    )
    assert r.status_code == 404
    assert client.get("/purchase-orders").json()[0]["status"] == "ordered"


@pytest.mark.parametrize(
    "body",
    [
        {"received_by": "x", "unit_cost": "0"},
        {"received_by": "x", "unit_cost": "1.234"},
        {"received_by": " ", "unit_cost": "1"},
        {"received_by": "x"},
    ],
)
def test_receipt_body_is_validated(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/purchase-orders/1/receive", json=body).status_code == 422
