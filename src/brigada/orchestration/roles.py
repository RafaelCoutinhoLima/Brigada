"""Planner, synthesizer and critic: single-purpose agents on the same runtime loop.

The planner and the critic answer through a terminal tool whose executor validates the input
with Pydantic; a rejected input goes back to the model as a tool error to fix, within a small
step budget. The synthesizer has no tools and answers in text.
"""

import hashlib
import json
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
SUBMIT_REVIEW = "submit_review"
_WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


@dataclass(frozen=True)
class Role:
    """A configured agent plus the hash of its stable prompt (stored with every run)."""

    agent: Agent
    prompt_sha256: str


def planner(client: ModelClient, restaurant_id: int, today: date) -> Role:
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
        lambda p: None,
    )


def critic(client: ModelClient, restaurant_id: int, today: date, done: frozenset[str]) -> Role:
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
    )


def synthesizer(client: ModelClient, restaurant_id: int, today: date) -> Role:
    settings = get_settings()
    prefix = load_prompt("synthesizer").text
    agent = Agent(
        AgentConfig(
            name="synthesizer",
            model=settings.model_strong,
            system=_with_context(prefix, restaurant_id, today),
            max_steps=1,
            max_tokens=2000,
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
            system=_with_context(prefix, restaurant_id, today),
            max_steps=3,
            max_tokens=2000,
            terminal_tools=frozenset({tool}),
            tool_choice={"type": "tool", "name": tool},
        ),
        client,
        [{"name": tool, "description": description, "input_schema": tool_schema(schema)}],
        validate,
    )
    return Role(agent, _sha(prefix + json.dumps(tool_schema(schema), sort_keys=True)))


def _with_context(prefix: str, restaurant_id: int, today: date) -> str:
    # Run context goes at the end so the stable instructions stay a cacheable prefix.
    return (
        f"{prefix}\n"
        f"Contexto desta conversa:\n"
        f"- restaurant_id: {restaurant_id}\n"
        f"- hoje: {today.isoformat()} ({_WEEKDAYS[today.weekday()]})\n"
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


async def _no_tools(call: ToolCall) -> str:
    raise ToolExecutionError(f"tool {call.name!r} is not available")
