"""The plan the planner proposes and the critic may extend: validated before anything runs."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_STEPS = 5
MAX_EXTRA_STEPS = 3

SpecialistName = Literal["sales", "menu", "operations"]


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^s[0-9]{1,2}$", description="Step id: s1, s2, ...")
    specialist: SpecialistName
    task: str = Field(
        min_length=1,
        max_length=600,
        description="What this specialist must find out, in Portuguese, keeping the owner's "
        "relative dates ('ontem', 'última terça') as written.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Ids of steps whose findings this step needs. Empty = runs right away.",
    )


class Plan(BaseModel):
    """Either steps for the specialists or, for questions outside the restaurant's data, a
    direct answer (never both)."""

    model_config = ConfigDict(extra="forbid")

    steps: list[PlanStep] = Field(default_factory=list, max_length=MAX_STEPS)
    direct_answer: str | None = Field(
        default=None,
        max_length=1000,
        description="Only when no specialist can help (greeting, out of scope). Portuguese.",
    )

    @model_validator(mode="after")
    def _check(self) -> "Plan":
        if bool(self.steps) == bool(self.direct_answer):
            raise ValueError("give either steps or direct_answer, exactly one of them")
        check_graph(self.steps)
        return self


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    issues: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="What is wrong with the answer: untraceable numbers, contradictions with "
        "the findings, missing parts of the question. Portuguese.",
    )
    extra_steps: list[PlanStep] = Field(
        default_factory=list,
        max_length=MAX_EXTRA_STEPS,
        description="Only when not approved and fixing it needs more data. New ids; "
        "depends_on may only name steps that already ran.",
    )

    @model_validator(mode="after")
    def _check(self) -> "Review":
        if self.approved and (self.issues or self.extra_steps):
            raise ValueError("an approved review has no issues and no extra_steps")
        if not self.approved and not self.issues:
            raise ValueError("a rejected review must list its issues")
        return self


def check_graph(steps: list[PlanStep], done: frozenset[str] = frozenset()) -> None:
    """Ids unique and new, dependencies known (in ``steps`` or already ``done``), no cycles."""
    ids = [s.id for s in steps]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate step ids: {ids}")
    if clash := set(ids) & done:
        raise ValueError(f"step ids already used: {sorted(clash)}")
    for s in steps:
        unknown = set(s.depends_on) - set(ids) - done
        if unknown:
            raise ValueError(f"step {s.id} depends on unknown steps: {sorted(unknown)}")
        if s.id in s.depends_on:
            raise ValueError(f"step {s.id} depends on itself")
    layers(steps)  # raises on cycles


def layers(steps: list[PlanStep]) -> list[list[PlanStep]]:
    """Topological layers: every step's in-plan dependencies sit in earlier layers, so the
    steps of one layer can run concurrently."""
    ids = {s.id for s in steps}
    pending = {s.id: {d for d in s.depends_on if d in ids} for s in steps}
    by_id = {s.id: s for s in steps}
    out: list[list[PlanStep]] = []
    while pending:
        ready = sorted(i for i, deps in pending.items() if not deps)
        if not ready:
            raise ValueError(f"dependency cycle between steps {sorted(pending)}")
        out.append([by_id[i] for i in ready])
        for i in ready:
            del pending[i]
        for deps in pending.values():
            deps.difference_update(ready)
    return out


def tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for a tool's ``input_schema``, with ``$ref``s inlined."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(schema)
