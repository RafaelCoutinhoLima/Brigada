"""A baseline answerer that always says it doesn't know (should score low)."""

from __future__ import annotations

from datetime import date

from evals.schema import AnswerResult


async def answer(question: str, restaurant_id: int, today: date) -> AnswerResult:
    return AnswerResult(text="não sei", steps=1)
