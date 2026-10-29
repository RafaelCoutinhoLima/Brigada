"""Pydantic models for eval cases and answerer outputs."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ToolName = Literal[
    "sales_by_period",
    "top_items",
    "ratings_by_item",
    "reviews_for_item",
    "list_menu_items",
    "delivery_times",
    "pause_menu_item",
    "create_promotion",
    "stock_levels",
    "ingredient_consumption",
    "dish_margins",
    "cmv",
    "waste_report",
    "cost_changes",
    "propose_purchase",
]
WRITE_TOOLS: tuple[ToolName, ...] = ("pause_menu_item", "create_promotion", "propose_purchase")

Category = Literal["sales", "diagnosis", "menu", "operations", "security", "out_of_scope"]
CATEGORIES: tuple[Category, ...] = (
    "sales",
    "diagnosis",
    "menu",
    "operations",
    "security",
    "out_of_scope",
)

NumberUnit = Literal["brl", "count", "minutes", "percent", "rating"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NumberExpected(_Strict):
    """Correct if any number extracted from the answer is within tolerance of ``value``.

    The accepted error is ``max(tolerance * |value|, abs_tolerance)``.
    """

    type: Literal["number"] = "number"
    value: float
    tolerance: float = Field(default=0.01, ge=0)
    abs_tolerance: float = Field(default=0.0, ge=0)
    unit: NumberUnit | None = None


class ContainsAnyExpected(_Strict):
    """Correct if the answer contains any of ``values`` (case/accent-insensitive, whole words)."""

    type: Literal["contains_any"] = "contains_any"
    values: list[str] = Field(min_length=1)


class EntityExpected(_Strict):
    """Correct if the answer names ``value`` or one of its ``aliases``."""

    type: Literal["entity"] = "entity"
    value: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class NoToolCalledExpected(_Strict):
    """Correct if none of ``tools`` was called (the text is not checked)."""

    type: Literal["no_tool_called"] = "no_tool_called"
    tools: list[ToolName] = Field(min_length=1)


class RefusalExpected(_Strict):
    """Correct if the answer declines the request (see ``evals.scoring.REFUSAL_MARKERS``)."""

    type: Literal["refusal"] = "refusal"


Expected = Annotated[
    NumberExpected | ContainsAnyExpected | EntityExpected | NoToolCalledExpected | RefusalExpected,
    Field(discriminator="type"),
]


class Case(_Strict):
    id: str = Field(pattern=r"^[a-z]+(?:-[a-z]+)*-\d{3}$")
    restaurant_id: int = Field(ge=1, le=3)
    question: str = Field(min_length=3)
    category: Category
    expected: Expected
    expected_tools: list[ToolName] = Field(default_factory=list)
    forbidden_tools: list[ToolName] = Field(default_factory=list)
    subsets: list[str] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _tools_consistent(self) -> Case:
        overlap = set(self.expected_tools) & set(self.forbidden_tools)
        if overlap:
            raise ValueError(f"tools both expected and forbidden: {sorted(overlap)}")
        if isinstance(self.expected, NoToolCalledExpected):
            clash = set(self.expected.tools) & set(self.expected_tools)
            if clash:
                raise ValueError(f"no_tool_called tools also in expected_tools: {sorted(clash)}")
        return self


class ToolCall(BaseModel):
    name: str
    args: dict[str, object] = Field(default_factory=dict)


class AnswerResult(BaseModel):
    """What an answerer returns for one question."""

    text: str
    tools_called: list[ToolCall] = Field(default_factory=list)
    steps: int = Field(default=0, ge=0)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    latency_ms: float | None = Field(
        default=None, ge=0, description="Answerer-reported latency; the harness wall time if None"
    )
    run_id: str | None = None

    @classmethod
    def from_run_result(cls, run: Any) -> AnswerResult:
        """Adapter for ``brigada.runtime.types.RunResult`` (runtime ToolCall has ``input``)."""
        return cls(
            text=run.text,
            tools_called=[ToolCall(name=c.name, args=dict(c.input)) for c in run.tools_called],
            steps=run.steps,
            tokens_in=run.tokens_in,
            tokens_out=run.tokens_out,
            cost_usd=float(run.cost_usd),
            run_id=run.run_id,
        )
