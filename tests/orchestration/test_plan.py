from decimal import Decimal

import pytest
from pydantic import ValidationError

from brigada.orchestration.numbers import numbers, untraced_numbers
from brigada.orchestration.plan import Plan, PlanStep, Review, check_graph, layers, tool_schema


def step(id: str, specialist: str = "sales", *deps: str) -> PlanStep:
    return PlanStep(id=id, specialist=specialist, task="t", depends_on=list(deps))  # type: ignore[arg-type]


def test_layers_put_independent_steps_together() -> None:
    plan = [
        step("s1"),
        step("s2", "menu"),
        step("s3", "operations", "s1", "s2"),
        step("s4", "sales", "s3"),
    ]
    assert [[s.id for s in layer] for layer in layers(plan)] == [["s1", "s2"], ["s3"], ["s4"]]


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([step("s1"), step("s1")], "duplicate"),
        ([step("s1", "sales", "s9")], "unknown"),
        ([step("s1", "sales", "s1")], "itself"),
        ([step("s1", "sales", "s2"), step("s2", "sales", "s1")], "cycle"),
    ],
)
def test_bad_graphs_are_rejected(steps: list[PlanStep], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_graph(steps)


def test_extra_steps_may_depend_on_done_steps_but_not_reuse_their_ids() -> None:
    check_graph([step("s3", "menu", "s1")], frozenset({"s1", "s2"}))
    with pytest.raises(ValueError, match="already used"):
        check_graph([step("s2")], frozenset({"s1", "s2"}))


def test_plan_needs_steps_or_direct_answer_not_both() -> None:
    with pytest.raises(ValidationError):
        Plan()
    with pytest.raises(ValidationError):
        Plan(steps=[step("s1")], direct_answer="oi")
    with pytest.raises(ValidationError, match="specialist"):
        Plan.model_validate({"steps": [{"id": "s1", "specialist": "chef", "task": "x"}]})
    assert Plan(direct_answer="Posso ajudar com vendas, cardápio e entregas.").steps == []


def test_review_shape() -> None:
    with pytest.raises(ValidationError):
        Review(approved=False)
    with pytest.raises(ValidationError):
        Review(approved=True, issues=["x"])
    assert Review(approved=False, issues=["número 3 sem origem"]).extra_steps == []


def test_tool_schema_has_no_refs() -> None:
    assert "$ref" not in str(tool_schema(Plan)) and "$defs" not in str(tool_schema(Review))


def test_numbers_read_both_formats_and_skip_dates() -> None:
    found = numbers("R$ 1.234,56 em 27/10 e 2026-10-27, 3 pedidos, 1.5x, 132.857")
    assert [values for _, values in found] == [
        {Decimal("1234.56")},
        {Decimal("3")},
        {Decimal("1.5")},
        {Decimal("132857"), Decimal("132.857")},
    ]


def test_untraced_numbers_accept_rounding() -> None:
    sources = ['{"total_revenue": 2496.6, "average_ticket": 132.857}', "terças: 6120.4"]
    assert untraced_numbers("R$ 2.496,60 contra R$ 6.120,40; ticket R$ 132,86", sources) == []
    assert untraced_numbers("Você vendeu R$ 9.999,00 com 41 pedidos", sources) == ["41", "9.999,00"]


def test_pt_br_thousands_in_the_answer_are_not_small_numbers() -> None:
    sources = ["Terça: 5 pedidos no app, nota média 2,5."]
    assert untraced_numbers("Faturamento de R$ 5.000 na terça", sources) == ["5.000"]
    assert untraced_numbers("R$ 2.500 de receita", sources) == ["2.500"]
    assert untraced_numbers("5 pedidos, nota 2,5", sources) == []


def test_dates_must_come_from_a_source() -> None:
    sources = ['{"start_date": "2026-10-27"}']
    assert untraced_numbers("Queda em 27/10", sources) == []
    assert untraced_numbers("Queda em 2026-10-21", sources) == ["2026-10-21"]
    assert untraced_numbers("Cancelaram 3/4 dos pedidos", sources) == ["3/4"]
    assert untraced_numbers("razão 12/40", ["12 e 40"]) == []  # not a date: two numbers


def test_huge_digit_runs_do_not_crash() -> None:
    boleto = "23793381286000782713695000063305975520000370000"
    assert (
        untraced_numbers("ok, 1 pedido", ["pedido 123456789012345678901234567", boleto, "1"]) == []
    )
    assert untraced_numbers(f"boleto {boleto}", ["1"]) == [boleto]


def test_planner_may_not_invent_dates() -> None:
    from brigada.orchestration.plan import Plan
    from brigada.orchestration.roles import dates_come_from_the_owner

    def plan_with(task: str) -> Plan:
        return Plan.model_validate(
            {"steps": [{"id": "s1", "specialist": "sales", "task": task, "depends_on": []}]}
        )

    question = "Por que minhas vendas caíram na última terça?"
    # The live bug: "última terça" on Wednesday 28/10 became 21/10.
    with pytest.raises(ValueError, match="2026-10-21"):
        dates_come_from_the_owner(
            plan_with("Compare a terça (2026-10-21) com 2026-10-14"), question
        )
    dates_come_from_the_owner(plan_with("Compare a última terça com a terça anterior"), question)
    # A date the owner wrote may be repeated.
    dates_come_from_the_owner(plan_with("Vendas de 15/10"), "Quanto vendi em 15/10?")
