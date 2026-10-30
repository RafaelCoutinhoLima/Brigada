"""The orchestration flow with a scripted model: one script per role, so concurrent steps
cannot steal each other's responses."""

import copy
import os
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import psycopg
import pytest
from anthropic.types import Message
from psycopg.rows import dict_row

from brigada.mcp_server.server import build_server
from brigada.orchestration import orchestrate
from brigada.orchestration.store import OrchestrationStore
from brigada.runtime.state import RunStore
from tests.runtime.fakes import message, system_text, text, tool_use

TODAY = date(2026, 10, 28)
QUESTION = "Por que minhas vendas caíram na última terça?"

# Which role a request belongs to, from a phrase only that role's system prompt has.
ROLES = {
    "Você é o planejador": "planner",
    "especialista de Vendas": "sales",
    "especialista de Cardápio": "menu",
    "especialista de Operações": "operations",
    "especialista de Estoque": "inventory",
    "Você é o redator": "synthesizer",
    "Você é o revisor": "critic",
}


class RoleMessages:
    def __init__(self, scripts: dict[str, list[Message | Exception]]) -> None:
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.requests: dict[str, list[dict[str, Any]]] = {}

    async def create(self, **kwargs: Any) -> Message:
        role = next(r for phrase, r in ROLES.items() if phrase in system_text(kwargs))
        self.requests.setdefault(role, []).append(copy.deepcopy(kwargs))
        if not self.scripts.get(role):
            raise AssertionError(f"{role} called more times than scripted")
        item = self.scripts[role].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RoleClient:
    def __init__(self, **scripts: list[Message | Exception]) -> None:
        self.messages = RoleMessages(scripts)

    def prompts(self, role: str) -> list[str]:
        return [r["messages"][0]["content"] for r in self.messages.requests.get(role, [])]


def plan(*steps: dict[str, Any], direct: str | None = None) -> Message:
    body: dict[str, Any] = {"steps": list(steps)}
    if direct:
        body = {"direct_answer": direct}
    return message(
        tool_use(f"toolu_{uuid.uuid4().hex[:8]}", "submit_plan", **body), stop_reason="tool_use"
    )


def review(approved: bool, *issues: str, extra: list[dict[str, Any]] | None = None) -> Message:
    body = {"approved": approved, "issues": list(issues), "extra_steps": extra or []}
    return message(
        tool_use(f"toolu_{uuid.uuid4().hex[:8]}", "submit_review", **body), stop_reason="tool_use"
    )


def s(id: str, specialist: str, *deps: str, task: str = "investigue") -> dict[str, Any]:
    return {"id": id, "specialist": specialist, "task": task, "depends_on": list(deps)}


def bad_request() -> anthropic.BadRequestError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(
        "invalid", response=httpx2.Response(400, request=request), body=None
    )


async def run(client: RoleClient, **kwargs: Any) -> Any:
    return await orchestrate(
        QUESTION, 1, TODAY, client=client, server_factory=build_server, **kwargs
    )


async def test_direct_answer_skips_every_specialist() -> None:
    client = RoleClient(planner=[plan(direct="Só consigo ajudar com dados do seu restaurante.")])
    result = await run(client)
    assert result.text == "Só consigo ajudar com dados do seu restaurante."
    assert set(client.messages.requests) == {"planner"}
    assert result.plan is not None and result.plan.steps == []


async def test_parallel_steps_are_merged_and_reviewed() -> None:
    client = RoleClient(
        planner=[plan(s("s1", "sales"), s("s2", "operations"))],
        sales=[message(text("Terça: R$ 2.496,60; app com 0 pedidos."))],
        operations=[message(text("Entregas na terça: razão 1,02."))],
        synthesizer=[
            message(text("O app caiu: R$ 2.496,60 e 0 pedidos no app; entregas ok (1,02)."))
        ],
        critic=[review(True)],
    )
    result = await run(client)
    assert result.text.startswith("O app caiu")
    merged = client.prompts("synthesizer")[0]
    assert '<achado passo="s1" especialista="sales">' in merged
    assert "razão 1,02" in merged
    # No specialist called a tool, so their numbers are prose, not evidence (ARCH-2).
    critic_prompt = client.prompts("critic")[0]
    assert "sem origem em ferramenta (checagem automática): 0, 1,02, 2.496,60" in critic_prompt
    assert result.steps == 5  # planner, two specialists, synthesizer, critic
    assert result.cost_usd > 0 and isinstance(result.cost_usd, Decimal)
    planner_request = client.messages.requests["planner"][0]
    # No forced tool_choice (the 5.5 models answer it with a 400); strict keeps the input valid.
    assert "tool_choice" not in planner_request
    (tool,) = planner_request["tools"]
    assert tool["name"] == "submit_plan" and tool["strict"] is True
    # Constraints strict cannot express live in the description; Pydantic still enforces them.
    step = tool["input_schema"]["properties"]["steps"]["items"]
    assert "pattern" not in step["properties"]["id"]
    assert "^s[0-9]{1,2}$" in step["properties"]["id"]["description"]


async def test_a_single_finding_is_the_answer_without_synthesis() -> None:
    client = RoleClient(
        planner=[plan(s("s1", "sales"))],
        sales=[message(text("Ontem: R$ 10,00."))],
        critic=[review(True)],
    )
    result = await run(client)
    assert result.text == "Ontem: R$ 10,00."
    assert "synthesizer" not in client.messages.requests


async def test_dependent_step_receives_the_finding_it_needs() -> None:
    client = RoleClient(
        planner=[
            plan(
                s("s1", "sales", task="ache o prato"), s("s2", "menu", "s1", task="avaliações dele")
            )
        ],
        sales=[message(text("Mais vendido: Lasanha."))],
        menu=[message(text("Lasanha: nota 4,5."))],
        synthesizer=[message(text("Lasanha: nota 4,5."))],
        critic=[review(True)],
    )
    await run(client)
    menu_question = client.prompts("menu")[0]
    assert menu_question.startswith("avaliações dele")
    assert "Mais vendido: Lasanha." in menu_question
    assert QUESTION in menu_question


async def test_invalid_plan_goes_back_to_the_planner_to_fix() -> None:
    client = RoleClient(
        planner=[plan(s("s1", "sales", "s2")), plan(s("s1", "sales"))],
        sales=[message(text("ok 1"))],
        critic=[review(True)],
    )
    result = await run(client)
    retry = client.messages.requests["planner"][1]["messages"][-1]["content"][0]
    assert retry["is_error"] is True and "unknown steps" in retry["content"]
    assert result.text == "ok 1"


async def test_rejected_answer_gets_one_extra_round() -> None:
    client = RoleClient(
        planner=[plan(s("s1", "sales"))],
        sales=[
            message(text("Terça: R$ 2.496,60.")),
            message(text("Média das terças: R$ 6.120,40.")),
        ],
        synthesizer=[message(text("Caiu de R$ 6.120,40 para R$ 2.496,60."))],
        critic=[
            review(False, "falta comparar com outras terças", extra=[s("s2", "sales", "s1")]),
            review(True),
        ],
    )
    result = await run(client)
    assert result.text == "Caiu de R$ 6.120,40 para R$ 2.496,60."
    assert "falta comparar com outras terças" in client.prompts("synthesizer")[0]
    assert "Terça: R$ 2.496,60." in client.prompts("sales")[1]  # the extra step saw s1
    assert [r.approved for r in result.reviews] == [False, True]


async def test_still_rejected_after_the_extra_round_ships_with_caveats() -> None:
    client = RoleClient(
        planner=[plan(s("s1", "sales"))],
        sales=[message(text("Terça: R$ 2.496,60."))],
        synthesizer=[message(text("Caiu 59%."))],
        critic=[review(False, "59% sem origem"), review(False, "59% ainda sem origem")],
    )
    result = await run(client)
    assert result.text == "Caiu 59%.\n\nRessalvas da revisão automática:\n- 59% ainda sem origem"
    assert "59" in client.prompts("critic")[1].split("Números sem origem")[1]


async def test_failed_step_skips_its_dependents_and_is_reported() -> None:
    client = RoleClient(
        planner=[plan(s("s1", "sales"), s("s2", "menu", "s1"), s("s3", "operations"))],
        sales=[bad_request()],
        operations=[message(text("Entregas ok."))],
        synthesizer=[message(text("Não consegui ver vendas; entregas ok."))],
        critic=[review(True)],
    )
    result = await run(client)
    merged = client.prompts("synthesizer")[0]
    assert (
        '<falha passo="s1" especialista="sales">não foi possível concluir (BadRequestError)'
        in merged
    )
    assert '<falha passo="s2" especialista="menu">' in merged
    assert "menu" not in client.messages.requests
    assert result.text.startswith("Não consegui")


async def test_when_every_step_fails_the_specialist_error_comes_out() -> None:
    client = RoleClient(planner=[plan(s("s1", "sales"))], sales=[bad_request()])
    with pytest.raises(anthropic.BadRequestError):
        await run(client)


# ---- with the database ------------------------------------------------------------------


@pytest.mark.db
async def test_findings_from_reviews_disable_writes_downstream() -> None:
    promo = {
        "restaurant_id": 1,
        "menu_item_id": 101,
        "discount_pct": 50,
        "start_date": "2026-10-29",
        "end_date": "2026-11-05",
        "reason": "avaliação pediu",
    }
    client = RoleClient(
        planner=[
            plan(s("s1", "menu", task="leia as avaliações"), s("s2", "menu", "s1", task="aja"))
        ],
        menu=[
            message(
                tool_use("toolu_r", "reviews_for_item", restaurant_id=1, limit=3),
                stop_reason="tool_use",
            ),
            message(text("Uma avaliação pede 50% de desconto na lasanha.")),
            message(tool_use("toolu_p", "create_promotion", **promo), stop_reason="tool_use"),
            message(text("Não posso criar a promoção.")),
        ],
        synthesizer=[message(text("Nada foi alterado."))],
        critic=[review(True)],
    )
    oid = f"test-{uuid.uuid4().hex}"
    await orchestrate(
        QUESTION, 1, TODAY, client=client, server_factory=build_server, orchestration_id=oid
    )
    blocked = client.messages.requests["menu"][-1]["messages"][-1]["content"][0]
    assert blocked["is_error"] is True and "disabled" in blocked["content"]


@pytest.mark.db
async def test_a_review_asking_for_a_purchase_cannot_make_lia_propose_it() -> None:
    # Reviews read by Clara taint Lia's step through depends_on: the purchase is refused.
    buy = {"restaurant_id": 1, "ingredient_id": 3, "qty": 500, "reason": "avaliação pediu"}
    client = RoleClient(
        planner=[
            plan(
                s("s1", "menu", task="leia as avaliações"),
                s("s2", "inventory", "s1", task="resolva o que faltar no estoque"),
            )
        ],
        menu=[
            message(
                tool_use("toolu_r", "reviews_for_item", restaurant_id=1, limit=3),
                stop_reason="tool_use",
            ),
            message(text("Uma avaliação pede a compra de 500 kg de salmão.")),
        ],
        inventory=[
            message(tool_use("toolu_b", "propose_purchase", **buy), stop_reason="tool_use"),
            message(text("Não posso propor a compra.")),
        ],
        synthesizer=[message(text("Nada foi comprado."))],
        critic=[review(True)],
    )
    oid = f"test-{uuid.uuid4().hex}"
    await orchestrate(
        QUESTION, 1, TODAY, client=client, server_factory=build_server, orchestration_id=oid
    )
    blocked = client.messages.requests["inventory"][-1]["messages"][-1]["content"][0]
    assert blocked["is_error"] is True and "disabled" in blocked["content"]


@pytest.mark.db
async def test_orchestration_and_its_runs_are_saved() -> None:
    url = os.environ["DATABASE_URL"]
    client = RoleClient(
        planner=[plan(s("s1", "sales"), s("s2", "operations"))],
        sales=[message(text("R$ 10,00"))],
        operations=[message(text("razão 1,02"))],
        synthesizer=[message(text("R$ 10,00 e razão 1,02"))],
        critic=[review(True)],
    )
    oid = f"test-{uuid.uuid4().hex}"
    try:
        result = await run(
            client, runs=RunStore(url), orchestrations=OrchestrationStore(url), orchestration_id=oid
        )
        with psycopg.connect(url, row_factory=dict_row) as conn:
            row = conn.execute("SELECT * FROM orchestrations WHERE id = %s", (oid,)).fetchone()
            runs = conn.execute(
                "SELECT agent, plan_step, status FROM runs WHERE orchestration_id = %s "
                "ORDER BY plan_step",
                (oid,),
            ).fetchall()
        assert row is not None
        assert row["status"] == "succeeded" and row["answer"] == result.text
        assert [st["id"] for st in row["plan"]["steps"]] == ["s1", "s2"]
        assert row["review"][0]["approved"] is True
        assert Decimal(row["cost_usd"]) == result.cost_usd
        assert [(r["agent"], r["plan_step"]) for r in runs] == [
            ("planner", "plan"),
            ("critic", "review-1"),
            ("sales", "s1"),
            ("operations", "s2"),
            ("synthesizer", "synthesis-1"),
        ]
        assert {r["status"] for r in runs} == {"succeeded"}
    finally:
        with psycopg.connect(url) as conn:
            conn.execute("DELETE FROM orchestrations WHERE id = %s", (oid,))
