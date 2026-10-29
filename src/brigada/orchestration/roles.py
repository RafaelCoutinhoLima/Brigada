"""Planner, synthesizer and critic: single-purpose agents on the same runtime loop.

The planner and the critic answer through a terminal tool whose executor validates the input
with Pydantic; a rejected input goes back to the model as a tool error to fix, within a small
step budget. The synthesizer has no tools and answers in text.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, ValidationError

from brigada.config import get_settings
from brigada.prompts import load_prompt
from brigada.runtime.agent import Agent, AgentConfig, ModelClient
from brigada.runtime.specialists import SPECIALISTS
from brigada.runtime.tools import ToolExecutionError
from brigada.runtime.types import ToolCall

from .plan import Plan, Review, check_graph, tool_schema

SUBMIT_PLAN = "submit_plan"
# Reasoning models spend output tokens thinking before they write: 2000 was not enough for the
# critic to reach its submit_review call (it ended in max_tokens with no content).
STRUCTURED_MAX_TOKENS = 8000
SUBMIT_REVIEW = "submit_review"
_WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


@dataclass(frozen=True)
class Role:
    """A configured agent plus the hash of its stable prompt (stored with every run)."""

    agent: Agent
    prompt_sha256: str


def planner(
    client: ModelClient,
    restaurant_id: int,
    today: date,
    restaurant_name: str | None = None,
    question: str = "",
) -> Role:
    catalog = "\n".join(
        f"- {s.name}: {s.description} Ferramentas: {', '.join(s.tools)}."
        for s in SPECIALISTS.values()
    )
    prefix = f"{load_prompt('planner').text}{catalog}\n"
    return _structured(
        "planner",
        get_settings().model_fast,
        prefix,
        restaurant_id,
        today,
        client,
        SUBMIT_PLAN,
        "Entrega o plano: passos para os especialistas ou uma resposta direta.",
        Plan,
        lambda plan: dates_come_from_the_owner(plan, question),
        restaurant_name,
    )


_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")


def dates_come_from_the_owner(plan: Plan, question: str) -> None:
    """Tasks keep the owner's relative dates ("última terça"): the specialists know today and
    resolve them. A date the planner computed itself is refused, because it gets them wrong
    (a live run turned "última terça" on a Wednesday 28/10 into 21/10 instead of 27/10)."""
    allowed = set(_DATE.findall(question))
    for step in plan.steps:
        invented = [d for d in _DATE.findall(step.task) if d not in allowed]
        if invented:
            raise ValueError(
                f"step {step.id} writes dates {invented} the owner did not; keep the owner's "
                "words for dates (e.g. 'última terça', 'ontem'), the specialist resolves them"
            )


def critic(
    client: ModelClient,
    restaurant_id: int,
    today: date,
    done: frozenset[str],
    restaurant_name: str | None = None,
) -> Role:
    def extra_steps_are_new(review: Review) -> None:
        check_graph(review.extra_steps, done)

    return _structured(
        "critic",
        get_settings().model_fast,
        load_prompt("critic").text,
        restaurant_id,
        today,
        client,
        SUBMIT_REVIEW,
        "Entrega o veredito da revisão da resposta final.",
        Review,
        extra_steps_are_new,
        restaurant_name,
    )


def synthesizer(
    client: ModelClient, restaurant_id: int, today: date, restaurant_name: str | None = None
) -> Role:
    settings = get_settings()
    prefix = load_prompt("synthesizer").text
    agent = Agent(
        AgentConfig(
            name="synthesizer",
            model=settings.model_strong,
            system=_with_context(prefix, restaurant_id, today, restaurant_name),
            max_steps=1,
            max_tokens=STRUCTURED_MAX_TOKENS,
        ),
        client,
        [],
        _no_tools,
    )
    return Role(agent, _sha(prefix))


def _structured[M: BaseModel](
    name: str,
    model: str,
    prefix: str,
    restaurant_id: int,
    today: date,
    client: ModelClient,
    tool: str,
    description: str,
    schema: type[M],
    extra_check: Callable[[M], None],
    restaurant_name: str | None = None,
) -> Role:
    async def validate(call: ToolCall) -> str:
        try:
            value = schema.model_validate(call.input)
            extra_check(value)
        except (ValidationError, ValueError) as exc:
            raise ToolExecutionError(f"invalid {tool} input, fix and call again: {exc}") from exc
        return value.model_dump_json()

    agent = Agent(
        AgentConfig(
            name=name,
            model=model,
            system=_with_context(prefix, restaurant_id, today, restaurant_name),
            max_steps=3,
            max_tokens=STRUCTURED_MAX_TOKENS,
            terminal_tools=frozenset({tool}),
            tool_choice={"type": "tool", "name": tool},
        ),
        client,
        [{"name": tool, "description": description, "input_schema": tool_schema(schema)}],
        validate,
    )
    return Role(agent, _sha(prefix + json.dumps(tool_schema(schema), sort_keys=True)))


def _with_context(prefix: str, restaurant_id: int, today: date, name: str | None) -> str:
    # Run context goes at the end so the stable instructions stay a cacheable prefix. The name
    # lets the planner notice a question about another restaurant.
    who = f"{name} (restaurant_id {restaurant_id})" if name else f"restaurant_id {restaurant_id}"
    return (
        f"{prefix}\n"
        f"Contexto desta conversa:\n"
        f"- restaurante do dono: {who}\n"
        f"- hoje: {today.isoformat()} ({_WEEKDAYS[today.weekday()]})\n"
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


async def _no_tools(call: ToolCall) -> str:
    raise ToolExecutionError(f"tool {call.name!r} is not available")
