"""Live console: the orchestrator's plan and review, plus one pane per specialist, fed from
the persisted ``orchestrations``/``runs``/``steps``.

``GET /console`` serves the page. The page polls ``GET /console/runs`` and dispatches
questions with ``POST /console/dispatch`` (through the orchestrator, or straight to chosen
specialists); both need an API key scoped to the restaurant.
"""

import asyncio
import logging
import uuid
from collections.abc import Coroutine
from datetime import date, datetime
from decimal import Decimal
from importlib import resources
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from brigada.api.auth import get_allowed_restaurants, require_restaurant
from brigada.config import Settings, get_settings
from brigada.orchestration import orchestrate
from brigada.orchestration.store import OrchestrationStore
from brigada.runtime import specialists
from brigada.runtime.state import RunStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/console", tags=["console"])

PREVIEW_CHARS = 400
AgentName = Literal["sales", "menu", "operations"]

# Background runs started from the console. Kept here so they are not garbage-collected
# mid-flight; each run records its own success or failure in `runs`.
_tasks: set[asyncio.Task[Any]] = set()


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=1000)
    restaurant_id: int = Field(gt=0)
    orchestrate: bool = Field(
        default=False, description="Let the planner choose the specialists (ignores agents)"
    )
    agents: list[AgentName] = Field(
        default_factory=lambda: ["sales", "menu", "operations"], min_length=1, max_length=3
    )
    today: date | None = Field(
        default=None, description="Reference date for 'ontem'... (default: today)"
    )


class DispatchedRun(BaseModel):
    agent: str
    run_id: str


class DispatchResponse(BaseModel):
    runs: list[DispatchedRun]
    orchestration_id: str | None = None


class AgentInfo(BaseModel):
    name: str
    description: str
    tools: list[str]


class Event(BaseModel):
    step: int
    kind: Literal["text", "call", "result"]
    at: datetime
    text: str = ""
    tool: str | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    size: int | None = None
    latency_ms: int | None = None


class RunView(BaseModel):
    id: str
    agent: str
    question: str
    status: str
    model: str
    steps: int
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    answer: str | None
    error_type: str | None
    started_at: datetime
    finished_at: datetime | None
    orchestration_id: str | None
    plan_step: str | None
    events: list[Event]


class StepView(BaseModel):
    """One step of the plan (or of the critic's extra round) and how it went."""

    id: str
    specialist: str
    task: str
    depends_on: list[str]
    extra: bool
    status: Literal["pending", "running", "succeeded", "failed", "skipped"]
    run_id: str | None


class RoleView(BaseModel):
    plan_step: str  # plan | synthesis-N | review-N
    agent: str
    status: str
    run_id: str


class OrchestrationView(BaseModel):
    id: str
    question: str
    status: str
    rounds: int
    direct_answer: str | None
    steps: list[StepView]
    roles: list[RoleView]
    reviews: list[dict[str, Any]]
    answer: str | None
    error_type: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    started_at: datetime
    finished_at: datetime | None


class ConsoleState(BaseModel):
    agents: list[AgentInfo]
    orchestrations: list[OrchestrationView]
    runs: list[RunView]


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def console_page() -> HTMLResponse:
    page = resources.files("brigada.api").joinpath("static/console.html")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@router.get("/runs", response_model=ConsoleState)
async def console_runs(
    restaurant_id: Annotated[int, Query(gt=0)],
    allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=30)] = 9,
) -> ConsoleState:
    require_restaurant(restaurant_id, allowed)
    async with await psycopg.AsyncConnection.connect(
        settings.database_url, row_factory=dict_row
    ) as conn:
        # The most recent runs, plus each agent's latest one so no pane goes blank when a busy
        # orchestration fills the recent list with planner/critic runs.
        cols = (
            "id, agent, question, status, model, steps, tokens_in, tokens_out, cost_usd, "
            "answer, error, started_at, finished_at, orchestration_id, plan_step"
        )
        cur = await conn.execute(
            f"(SELECT {cols} FROM runs WHERE restaurant_id = %s ORDER BY started_at DESC "
            f"LIMIT %s) UNION (SELECT DISTINCT ON (agent) {cols} FROM runs "
            f"WHERE restaurant_id = %s ORDER BY agent, started_at DESC) "
            f"ORDER BY started_at DESC",
            (restaurant_id, limit, restaurant_id),
        )
        runs = await cur.fetchall()
        cur = await conn.execute(
            "SELECT id, question, status, rounds, plan, review, answer, error, tokens_in, "
            "tokens_out, cost_usd, started_at, finished_at FROM orchestrations "
            "WHERE restaurant_id = %s ORDER BY started_at DESC LIMIT 3",
            (restaurant_id,),
        )
        orchestrations = await cur.fetchall()
        cur = await conn.execute(
            "SELECT id, agent, status, orchestration_id, plan_step FROM runs "
            "WHERE orchestration_id = ANY(%s) ORDER BY started_at",
            ([o["id"] for o in orchestrations],),
        )
        linked = await cur.fetchall()
        cur = await conn.execute(
            "SELECT run_id, step, kind, payload, latency_ms, created_at FROM steps "
            "WHERE run_id = ANY(%s) ORDER BY run_id, step, id",
            ([r["id"] for r in runs],),
        )
        steps = await cur.fetchall()
    events: dict[str, list[Event]] = {r["id"]: [] for r in runs}
    tool_names: dict[str, str] = {}
    for s in steps:
        events[s["run_id"]].extend(_events(s, tool_names))
    return ConsoleState(
        orchestrations=[
            _orchestration_view(o, [r for r in linked if r["orchestration_id"] == o["id"]])
            for o in orchestrations
        ],
        agents=[
            AgentInfo(name=s.name, description=s.description, tools=list(s.tools))
            for s in specialists.SPECIALISTS.values()
        ],
        runs=[
            RunView(
                **{k: r[k] for k in RunView.model_fields if k in r},
                # The stored error can carry upstream details; the console only gets its type.
                error_type=_error_type(r["error"]),
                events=events[r["id"]],
            )
            for r in runs
        ],
    )


@router.post("/dispatch", response_model=DispatchResponse, status_code=status.HTTP_202_ACCEPTED)
async def console_dispatch(
    body: DispatchRequest,
    allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DispatchResponse:
    """Start the orchestrator, or one run per chosen specialist in parallel. Returns at once;
    poll /console/runs."""
    require_restaurant(body.restaurant_id, allowed)
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ANTHROPIC_API_KEY is not configured on the server",
        )
    today = body.today or datetime.now(ZoneInfo(settings.timezone)).date()
    store = RunStore(settings.database_url)
    if body.orchestrate:
        oid = uuid.uuid4().hex
        _start(
            orchestrate(
                body.question,
                body.restaurant_id,
                today,
                runs=store,
                orchestrations=OrchestrationStore(settings.database_url),
                orchestration_id=oid,
            ),
            f"console:orchestrator:{oid}",
        )
        return DispatchResponse(runs=[], orchestration_id=oid)
    dispatched: list[DispatchedRun] = []
    for name in dict.fromkeys(body.agents):  # dedupe, keep order
        run_id = uuid.uuid4().hex
        _start(
            specialists.ask(
                specialists.get_specialist(name),
                body.question,
                body.restaurant_id,
                today,
                run_id=run_id,
                store=store,
            ),
            f"console:{name}:{run_id}",
        )
        dispatched.append(DispatchedRun(agent=name, run_id=run_id))
    return DispatchResponse(runs=dispatched)


def _start(work: Coroutine[Any, Any, Any], name: str) -> None:
    task = asyncio.create_task(work, name=name)
    _tasks.add(task)
    task.add_done_callback(_finished)


def _finished(task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.warning("console run %s failed: %s", task.get_name(), type(exc).__name__)


def _events(step: dict[str, Any], tool_names: dict[str, str]) -> list[Event]:
    """Flatten one persisted step into what a pane prints, previews capped."""
    base = {"step": step["step"], "at": step["created_at"]}
    out: list[Event] = []
    if step["kind"] == "llm_call":
        for block in step["payload"].get("content", []):
            if block.get("type") == "text" and block.get("text"):
                out.append(Event(kind="text", text=block["text"], **base))
            elif block.get("type") == "tool_use":
                tool_names[block["id"]] = block["name"]
                out.append(
                    Event(
                        kind="call",
                        tool=block["name"],
                        tool_use_id=block["id"],
                        text=_preview(block.get("input", {})),
                        latency_ms=step["latency_ms"],
                        **base,
                    )
                )
    else:
        for result in step["payload"]:
            content = str(result.get("content", ""))
            out.append(
                Event(
                    kind="result",
                    tool=tool_names.get(result.get("tool_use_id", "")),
                    tool_use_id=result.get("tool_use_id"),
                    is_error=bool(result.get("is_error")),
                    text=content[:PREVIEW_CHARS],
                    size=len(content),
                    **base,
                )
            )
    return out


def _error_type(error: str | None) -> str | None:
    # The stored error can carry upstream details; the console only gets its type.
    return error.split(":", 1)[0] if error else None


def _orchestration_view(o: dict[str, Any], runs: list[dict[str, Any]]) -> OrchestrationView:
    by_step = {r["plan_step"]: r for r in runs}  # a later run of the same step wins
    plan = o["plan"] or {}
    reviews: list[dict[str, Any]] = o["review"] or []
    planned = [(st, False) for st in plan.get("steps", [])] + [
        (st, True) for rv in reviews for st in rv.get("extra_steps", [])
    ]
    finished = o["status"] != "running"
    steps = []
    for st, extra in planned:
        run = by_step.get(st["id"])
        state = run["status"] if run else ("skipped" if finished else "pending")
        steps.append(
            StepView(
                id=st["id"],
                specialist=st["specialist"],
                task=st["task"],
                depends_on=st.get("depends_on", []),
                extra=extra,
                status=state,
                run_id=run["id"] if run else None,
            )
        )
    step_ids = {st["id"] for st, _ in planned}
    roles = [
        RoleView(plan_step=r["plan_step"], agent=r["agent"], status=r["status"], run_id=r["id"])
        for r in runs
        if r["plan_step"] not in step_ids
    ]
    return OrchestrationView(
        id=o["id"],
        question=o["question"],
        status=o["status"],
        rounds=o["rounds"],
        direct_answer=plan.get("direct_answer"),
        steps=steps,
        roles=roles,
        reviews=reviews,
        answer=o["answer"],
        error_type=_error_type(o["error"]),
        tokens_in=o["tokens_in"],
        tokens_out=o["tokens_out"],
        cost_usd=o["cost_usd"],
        started_at=o["started_at"],
        finished_at=o["finished_at"],
    )


def _preview(value: Any) -> str:
    text = (
        ", ".join(f"{k}={v}" for k, v in value.items()) if isinstance(value, dict) else str(value)
    )
    return text[:PREVIEW_CHARS]
