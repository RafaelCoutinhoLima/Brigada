"""FastAPI application: ``uvicorn brigada.api.main:app``."""

from __future__ import annotations

import hmac
import importlib
import inspect
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, cast
from zoneinfo import ZoneInfo

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from brigada import __version__
from brigada.api.schemas import Answerer, AnswerOutcome, AskRequest, AskResponse, HealthResponse
from brigada.config import Settings, get_settings
from brigada.observability.tracing import current_trace_id, get_tracer, setup_tracing

logger = logging.getLogger(__name__)

ORCHESTRATOR_PACKAGE = "brigada.orchestration"
ORCHESTRATOR_MODULE = "brigada.orchestration.answer"
REQUEST_ID_HEADER = "X-Request-ID"
TRACE_ID_HEADER = "X-Trace-ID"
DB_CONNECT_TIMEOUT_SECONDS = 2


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    get_settings().api_key_scopes()  # malformed API_KEYS fails at boot, not per request
    setup_tracing(service_name="brigada-api")
    yield


app = FastAPI(title="Brigada", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def trace_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Echo request id and trace id as response headers.

    Recent FastAPI versions already open a server span per request; we annotate that span. If
    none is active (older FastAPI), we open our own so every request still has a trace id.
    """
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    if trace.get_current_span().get_span_context().is_valid:
        return await _annotate(request_id, call_next, request)
    with get_tracer().start_as_current_span(
        f"{request.method} {request.url.path}",
        kind=trace.SpanKind.SERVER,
        attributes={"http.request.method": request.method, "url.path": request.url.path},
    ) as span:
        response = await _annotate(request_id, call_next, request)
        span.set_attribute("http.response.status_code", response.status_code)
        if response.status_code >= 500:
            span.set_status(Status(StatusCode.ERROR, f"HTTP {response.status_code}"))
        return response


async def _annotate(
    request_id: str, call_next: Callable[[Request], Awaitable[Response]], request: Request
) -> Response:
    trace.get_current_span().set_attribute("brigada.request_id", request_id)
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    trace_id = current_trace_id()
    if trace_id is not None:
        response.headers[TRACE_ID_HEADER] = trace_id
    return response


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@app.get("/health/db")
def health_db(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, str]:
    try:
        with psycopg.connect(
            settings.database_url, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS
        ) as conn:
            conn.execute("SELECT 1").fetchone()
    except psycopg.Error as exc:
        logger.exception("Database health check failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "error": "database unavailable"},
        ) from exc
    return {"status": "ok"}


def _not_implemented(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            f"Orchestrator not implemented yet ({reason}). Expected "
            f"`{ORCHESTRATOR_PACKAGE}:answer` (or `{ORCHESTRATOR_MODULE}:answer`) with signature "
            "`[async] answer(question: str, restaurant_id: int, today: date)` returning an object "
            "with `text` and `cost_usd` (same contract as evals/run.py)."
        ),
    )


def get_answerer() -> Answerer:
    """Resolve the orchestrator lazily so the API boots before it exists.

    Looks for ``answer`` in ``brigada.orchestration`` (the evals harness default,
    ``brigada.orchestration:answer``), then in a ``brigada.orchestration.answer`` submodule.
    Only "not there yet" maps to 501; any other import error (e.g. a broken import *inside* the
    orchestrator module) propagates as a real 500.
    """
    package = importlib.import_module(ORCHESTRATOR_PACKAGE)
    fn = getattr(package, "answer", None)
    if fn is None or inspect.ismodule(fn):
        try:
            module = importlib.import_module(ORCHESTRATOR_MODULE)
        except ModuleNotFoundError as exc:
            if exc.name != ORCHESTRATOR_MODULE:
                raise
            raise _not_implemented(f"no `answer` in {ORCHESTRATOR_PACKAGE}") from exc
        fn = getattr(module, "answer", None)
        if fn is None:
            raise _not_implemented(f"{ORCHESTRATOR_MODULE} has no `answer`")
    if not callable(fn):
        raise RuntimeError(f"Orchestrator `answer` is not callable: {fn!r}")
    return cast(Answerer, fn)


def _today(settings: Settings) -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def get_allowed_restaurants(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> frozenset[int]:
    """Resolve the caller's API key to the restaurant ids it may query. Fails closed."""
    if x_api_key:
        for key, restaurant_ids in settings.api_key_scopes().items():
            if hmac.compare_digest(key.encode(), x_api_key.encode()):
                return restaurant_ids
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid X-API-Key",
        headers={"WWW-Authenticate": "ApiKey"},
    )


@app.post("/ask", response_model=AskResponse)
async def ask(
    body: AskRequest,
    allowed: Annotated[frozenset[int], Depends(get_allowed_restaurants)],
    answerer: Annotated[Answerer, Depends(get_answerer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AskResponse:
    if body.restaurant_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="API key not allowed for this restaurant"
        )
    started = time.perf_counter()
    result = answerer(body.question, body.restaurant_id, _today(settings))
    outcome: AnswerOutcome = await result if inspect.isawaitable(result) else result
    trace_id = current_trace_id()
    if trace_id is None:
        raise RuntimeError("No active trace: tracing is not configured")
    return AskResponse(
        answer=outcome.text,
        trace_id=trace_id,
        run_id=getattr(outcome, "run_id", None),
        cost_usd=Decimal(str(outcome.cost_usd)),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


# TODO: human-approval routes for pending MCP actions go here, delegating to
# brigada.mcp_server.actions: list_actions(status="pending"), approve(action_id, approved_by,
# note=None), reject(action_id, rejected_by, note=None). Not wired yet.
