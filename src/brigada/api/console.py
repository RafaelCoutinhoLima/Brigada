"""Live console: one pane per specialist, fed from the persisted ``runs``/``steps``.

``GET /console`` serves the page. The page polls ``GET /console/runs`` and dispatches
questions with ``POST /console/dispatch``; both need an API key scoped to the restaurant.
"""

import asyncio
import logging
import uuid
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
    agents: list[AgentName] = Field(min_length=1, max_length=3)
    today: date | None = Field(
        default=None, description="Reference date for 'ontem'... (default: today)"
    )


class DispatchedRun(BaseModel):
    agent: str
    run_id: str


class DispatchResponse(BaseModel):
    runs: list[DispatchedRun]


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
    events: list[Event]


class ConsoleState(BaseModel):
    agents: list[AgentInfo]
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
        cur = await conn.execute(
            "SELECT id, agent, question, status, model, steps, tokens_in, tokens_out, cost_usd, "
            "answer, error, started_at, finished_at FROM runs WHERE restaurant_id = %s "
            "ORDER BY started_at DESC LIMIT %s",
            (restaurant_id, limit),
        )
        runs = await cur.fetchall()
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
        agents=[
            AgentInfo(name=s.name, description=s.description, tools=list(s.tools))
            for s in specialists.SPECIALISTS.values()
        ],
        runs=[
            RunView(
                **{k: r[k] for k in RunView.model_fields if k in r},
                # The stored error can carry upstream details; the console only gets its type.
                error_type=r["error"].split(":", 1)[0] if r["error"] else None,
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
    """Start one run per chosen specialist, in parallel. Returns at once; poll /console/runs."""
    require_restaurant(body.restaurant_id, allowed)
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ANTHROPIC_API_KEY is not configured on the server",
        )
    today = body.today or datetime.now(ZoneInfo(settings.timezone)).date()
    store = RunStore(settings.database_url)
    dispatched: list[DispatchedRun] = []
    for name in dict.fromkeys(body.agents):  # dedupe, keep order
        run_id = uuid.uuid4().hex
        task = asyncio.create_task(
            specialists.ask(
                specialists.get_specialist(name),
                body.question,
                body.restaurant_id,
                today,
                run_id=run_id,
                store=store,
            ),
            name=f"console:{name}:{run_id}",
        )
        _tasks.add(task)
        task.add_done_callback(_finished)
        dispatched.append(DispatchedRun(agent=name, run_id=run_id))
    return DispatchResponse(runs=dispatched)


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


def _preview(value: Any) -> str:
    text = (
        ", ".join(f"{k}={v}" for k, v in value.items()) if isinstance(value, dict) else str(value)
    )
    return text[:PREVIEW_CHARS]
