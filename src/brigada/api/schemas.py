"""Request/response models and the answerer contract for the HTTP API."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import date
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=1000)
    restaurant_id: int = Field(gt=0)


class AskResponse(BaseModel):
    answer: str
    trace_id: str = Field(description="OpenTelemetry trace id (32 hex chars)")
    run_id: str | None = Field(
        description="Orchestrator run id, null if the orchestrator does not report one"
    )
    cost_usd: Decimal = Field(ge=0, description="Total Claude API cost of this run, in USD")
    latency_ms: int = Field(ge=0)


class HealthResponse(BaseModel):
    status: str
    version: str


class AnswerOutcome(Protocol):
    """What the orchestrator returns: the same shape as ``evals.schema.AnswerResult``.

    ``run_id`` is optional; it is surfaced in the API response when present.
    """

    @property
    def text(self) -> str: ...

    @property
    def cost_usd(self) -> float | Decimal: ...


class Answerer(Protocol):
    """Contract shared with evals/run.py: ``[async] answer(question, restaurant_id, today)``."""

    def __call__(
        self, question: str, restaurant_id: int, today: date, /
    ) -> AnswerOutcome | Awaitable[AnswerOutcome]: ...
