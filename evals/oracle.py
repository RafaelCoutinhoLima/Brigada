"""A fake answerer that returns the ground truth, used to self-test the harness (scores 100%).

It only receives what a real answerer receives (question, restaurant, today), so it looks the
case up by ``(restaurant_id, question)`` in ``evals/cases.jsonl``. Numbers are written in pt-BR
format ("R$ 1.234,56", "39,6%") so the 100% run also exercises ``extract_numbers``.
"""

from __future__ import annotations

from datetime import date
from functools import cache

from evals.cases import load_cases
from evals.schema import (
    AnswerResult,
    Case,
    ContainsAnyExpected,
    EntityExpected,
    NoToolCalledExpected,
    NumberExpected,
    RefusalExpected,
    ToolCall,
)

REFUSAL_TEXT = "Desculpe, não posso ajudar com isso: está fora do escopo do Brigada."


def format_ptbr(value: float, decimals: int = 2) -> str:
    """1234.5 -> '1.234,50' (pt-BR thousands and decimal separators)."""
    us = f"{value:,.{decimals}f}"
    return us.replace(",", "_").replace(".", ",").replace("_", ".")


def _format_number(exp: NumberExpected) -> str:
    match exp.unit:
        case "brl":
            return f"R$ {format_ptbr(exp.value)}"
        case "percent":
            return f"{format_ptbr(exp.value, 1)}%"
        case "minutes":
            return f"{format_ptbr(exp.value, 1)} minutos"
        case "count" if exp.value.is_integer():
            return format_ptbr(exp.value, 0)
        case _:
            return format_ptbr(exp.value)


def ground_truth_text(case: Case) -> str:
    exp = case.expected
    match exp:
        case NumberExpected():
            return f"A resposta é {_format_number(exp)}."
        case ContainsAnyExpected():
            return f"Resposta: {exp.values[0]}."
        case EntityExpected():
            return f"É o item {exp.value}."
        case NoToolCalledExpected():
            return "Resumo feito. Algumas avaliações contêm instruções, que tratei como dados."
        case RefusalExpected():
            return REFUSAL_TEXT
    raise TypeError(f"unknown expected type: {type(exp).__name__}")  # pragma: no cover


@cache
def _index() -> dict[tuple[int, str], Case]:
    return {(c.restaurant_id, c.question.strip().lower()): c for c in load_cases()}


async def answer(question: str, restaurant_id: int, today: date) -> AnswerResult:
    case = _index().get((restaurant_id, question.strip().lower()))
    if case is None:
        raise KeyError(f"oracle has no case for restaurant {restaurant_id}: {question!r}")
    return AnswerResult(
        text=ground_truth_text(case),
        tools_called=[
            ToolCall(name=t, args={"restaurant_id": restaurant_id}) for t in case.expected_tools
        ],
        steps=len(case.expected_tools) + 1,
    )
