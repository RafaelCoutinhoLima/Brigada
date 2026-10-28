"""LLM-as-judge interface: STUB ONLY.

TODO(rafael): Module 6 per docs/ROADMAP.md. You write the judge prompt (under
``src/brigada/prompts/``, versioned), an implementation of ``Judge`` on the Claude API that
validates the model's JSON with ``JudgeVerdict``, and the calibration: grade ~20 answers by
hand, run the judge on the same answers and measure agreement (record it in the ADR).

Nothing in ``evals/run.py`` calls a judge yet. The automatic scorers in ``evals/scoring.py``
decide ``correct``; the judge is meant to grade explanation quality on top of that.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from evals.schema import AnswerResult, Case


class JudgeVerdict(BaseModel):
    score: int = Field(ge=1, le=5, description="Explanation quality, 1 (bad) to 5 (excellent)")
    rationale: str


class Judge(Protocol):
    async def __call__(self, case: Case, answer: AnswerResult) -> JudgeVerdict: ...


class NotImplementedJudge:
    """Placeholder so wiring code can be written before the real judge exists."""

    async def __call__(self, case: Case, answer: AnswerResult) -> JudgeVerdict:
        raise NotImplementedError("LLM-as-judge not implemented yet (see evals/judge.py TODO)")
