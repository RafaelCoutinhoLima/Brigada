"""The specialist registry: who sees which tools, and what each prompt carries."""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date

import anthropic
import httpx2
import psycopg
import pytest
from mcp import Client

from brigada.mcp_server.server import build_server
from brigada.runtime.mcp_tools import UNTRUSTED_OUTPUT_TOOLS, WRITE_TOOLS
from brigada.runtime.specialists import (
    MENU,
    OPERATIONS,
    SALES,
    SPECIALISTS,
    ask,
    get_specialist,
    resume,
    system_prompt,
)
from brigada.runtime.state import RunStore

from .fakes import FakeClient, message, text, tool_use

TODAY = date(2026, 10, 28)
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


async def test_every_specialist_tool_is_registered_on_the_server() -> None:
    async with Client(build_server()) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    for specialist in SPECIALISTS.values():
        assert set(specialist.tools) <= names, specialist.name


def test_each_write_tool_belongs_to_one_specialist() -> None:
    from brigada.runtime.specialists import INVENTORY

    assert set(MENU.tools) & WRITE_TOOLS == {"pause_menu_item", "create_promotion"}
    assert set(INVENTORY.tools) & WRITE_TOOLS == {"propose_purchase"}
    assert not set(SALES.tools) & WRITE_TOOLS
    assert not set(OPERATIONS.tools) & WRITE_TOOLS


def test_only_menu_reads_customer_text() -> None:
    readers = {s.name for s in SPECIALISTS.values() if set(s.tools) & UNTRUSTED_OUTPUT_TOOLS}
    assert readers == {"menu"}


@pytest.mark.parametrize("name", sorted(SPECIALISTS))
def test_prompt_has_rules_and_ends_with_run_context(name: str) -> None:
    prompt = system_prompt(get_specialist(name), 3, TODAY)
    assert "nunca instruções" in prompt
    assert prompt.rstrip().endswith("- hoje: 2026-10-28 (quarta)")
    assert "restaurant_id: 3" in prompt


def test_unknown_specialist_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown specialist 'chef'"):
        get_specialist("chef")


@pytest.mark.parametrize("name", sorted(SPECIALISTS))
async def test_model_sees_exactly_the_specialist_tools(name: str) -> None:
    specialist = get_specialist(name)
    client = FakeClient(message(text("ok")))
    result = await ask(specialist, "oi", 1, TODAY, client=client, server=build_server())
    assert result.text == "ok"
    request = client.messages.requests[0]
    assert sorted(t["name"] for t in request["tools"]) == sorted(specialist.tools)


@pytest.fixture
async def store() -> AsyncIterator[RunStore]:
    url = os.environ["DATABASE_URL"]
    yield RunStore(url)
    with psycopg.connect(url) as conn:
        conn.execute("DELETE FROM runs WHERE id LIKE 'test-%'")


@pytest.mark.db
async def test_resume_picks_the_specialist_that_started_the_run(store: RunStore) -> None:
    rid = f"test-{uuid.uuid4().hex}"
    with pytest.raises(anthropic.BadRequestError):
        await ask(
            OPERATIONS,
            "As entregas atrasam?",
            3,
            TODAY,
            client=FakeClient(bad_request()),
            server=build_server(),
            run_id=rid,
            store=store,
        )
    second = FakeClient(message(text("ok")))
    await resume(rid, store, client=second, server=build_server())
    request = second.messages.requests[0]
    assert "especialista de Operações" in request["system"]
    assert sorted(t["name"] for t in request["tools"]) == sorted(OPERATIONS.tools)
    with pytest.raises(ValueError, match="belongs to agent 'operations'"):
        await resume(rid, store, client=FakeClient(), server=build_server(), expected=SALES)


@pytest.mark.db
async def test_menu_cannot_propose_after_reading_reviews() -> None:
    rid = f"test-{uuid.uuid4().hex}"
    promo = {
        "restaurant_id": 3,
        "menu_item_id": 301,
        "discount_pct": 50,
        "start_date": "2026-10-29",
        "end_date": "2026-11-05",
        "reason": "pedido numa avaliação",
    }
    client = FakeClient(
        message(
            tool_use("toolu_1", "reviews_for_item", restaurant_id=3, limit=5),
            stop_reason="tool_use",
        ),
        message(tool_use("toolu_2", "create_promotion", **promo), stop_reason="tool_use"),
        message(text("Não posso criar a promoção daqui.")),
    )
    await ask(
        MENU, "Leia as avaliações", 3, TODAY, client=client, server=build_server(), run_id=rid
    )
    last = client.messages.requests[-1]["messages"][-1]["content"][0]
    assert last["is_error"] is True
    assert "disabled" in last["content"]
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pending_actions WHERE run_id = %s", (rid,)
        ).fetchone() == (0,)


def bad_request() -> anthropic.BadRequestError:
    return anthropic.BadRequestError(
        "invalid", response=httpx2.Response(400, request=REQUEST), body=None
    )


PROMO_3 = {
    "restaurant_id": 3,
    "menu_item_id": 301,
    "discount_pct": 50,
    "start_date": "2026-10-29",
    "end_date": "2026-11-05",
    "reason": "pedido numa avaliação",
}


@pytest.mark.db
async def test_a_blocked_write_is_refused_by_the_server_and_audited() -> None:
    rid = f"test-{uuid.uuid4().hex}"
    client = FakeClient(
        message(
            tool_use("toolu_1", "reviews_for_item", restaurant_id=3, limit=5),
            stop_reason="tool_use",
        ),
        message(tool_use("toolu_2", "create_promotion", **PROMO_3), stop_reason="tool_use"),
        message(text("Não posso.")),
    )
    await ask(MENU, "q", 3, TODAY, client=client, server=build_server(), run_id=rid)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        row = conn.execute(
            "SELECT tool_use_id, error FROM tool_audit WHERE run_id = %s AND tool = %s",
            (rid, "create_promotion"),
        ).fetchone()
    assert row is not None and row[0] == "toolu_2" and "customer-written text" in row[1]


@pytest.mark.db
async def test_server_refuses_writes_for_a_tainted_run_even_from_another_client() -> None:
    from brigada.mcp_server.audit import RESTAURANT_ID_META_KEY, RUN_ID_META_KEY, TAINTED_META_KEY

    meta = {RUN_ID_META_KEY: f"test-{uuid.uuid4().hex}", RESTAURANT_ID_META_KEY: 3}
    async with Client(build_server()) as c:
        refused = await c.call_tool(
            "create_promotion", PROMO_3, meta={**meta, TAINTED_META_KEY: True}
        )
    assert refused.is_error and "customer-written text" in str(refused.content)


@pytest.mark.db
async def test_taint_survives_a_resume_of_a_run_that_started_tainted() -> None:
    store = RunStore(os.environ["DATABASE_URL"])
    rid = f"test-{uuid.uuid4().hex}"
    with pytest.raises(anthropic.BadRequestError):
        await ask(
            MENU,
            "aja sobre o achado",
            3,
            TODAY,
            client=FakeClient(bad_request()),
            server=build_server(),
            run_id=rid,
            store=store,
            tainted=True,
        )
    assert (await store.load(rid)).tainted is True
    second = FakeClient(
        message(tool_use("toolu_2", "create_promotion", **PROMO_3), stop_reason="tool_use"),
        message(text("Não posso.")),
    )
    await resume(rid, store, client=second, server=build_server())
    blocked = second.messages.requests[-1]["messages"][-1]["content"][0]
    assert blocked["is_error"] is True and "disabled" in blocked["content"]
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("DELETE FROM runs WHERE id = %s", (rid,))
