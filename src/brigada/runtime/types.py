"""Data types and errors shared by the agent runtime. No logic lives here."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """One ``tool_use`` block the model asked for."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    """What goes back to the model as one ``tool_result`` block."""

    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class RunResult:
    """Final answer of one agent run plus what it cost to get there."""

    text: str
    steps: int
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    tools_called: list[ToolCall] = field(default_factory=list)
    run_id: str | None = None
    # What the tools returned (successful results only): the ground truth a reviewer can
    # check the answer's numbers against, as opposed to the model's own prose.
    tool_outputs: list[str] = field(default_factory=list)


class AgentError(RuntimeError):
    """Base class: every way a run can end without a valid answer.

    ``run_id`` is filled in when the run was persisted, so the caller can resume it.
    ``partial`` (set on any exception leaving ``Agent.resume``) is what the run had spent and
    called before failing, so totals and security checks do not lose it.
    """

    run_id: str | None = None
    partial: "RunResult | None" = None


class MaxStepsExceeded(AgentError):
    def __init__(self, max_steps: int) -> None:
        super().__init__(f"agent did not finish within {max_steps} model calls")
        self.max_steps = max_steps


class NoStructuredAnswer(MaxStepsExceeded):
    """A structured role spent its budget answering in prose instead of calling its terminal
    tool. A ``MaxStepsExceeded`` (resumable with a fresh budget); ``text`` keeps the last
    prose for whoever debugs it."""

    def __init__(self, max_steps: int, tools: frozenset[str], text: str) -> None:
        AgentError.__init__(
            self,
            f"agent answered in prose instead of calling {', '.join(sorted(tools))} "
            f"within {max_steps} model calls",
        )
        self.max_steps = max_steps
        self.text = text


class ModelRefused(AgentError):
    def __init__(self, category: str | None, explanation: str | None) -> None:
        super().__init__(f"model refused (category={category}): {explanation}")
        self.category = category
        self.explanation = explanation


class ResponseTruncated(AgentError):
    """``stop_reason == "max_tokens"``: the answer was cut mid-way, so it is not an answer."""


class UnexpectedStopReason(AgentError):
    def __init__(self, stop_reason: str | None) -> None:
        super().__init__(f"unexpected stop_reason: {stop_reason!r}")
        self.stop_reason = stop_reason
