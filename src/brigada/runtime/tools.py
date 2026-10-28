"""Run the tools of one model turn concurrently, each under its own timeout.

A tool failure never aborts the turn: it becomes a ``tool_result`` with ``is_error=True`` so
the model sees what went wrong and can react (retry with other arguments, explain, give up).
Cancellation of the whole run is the exception and propagates.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from brigada.runtime.types import ToolCall, ToolOutcome

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[ToolCall], Awaitable[str]]
"""Runs one tool and returns its result as text (usually JSON). Raises on failure."""


MAX_ERROR_CHARS = 1000


class ToolExecutionError(Exception):
    """A tool ran and reported an error (e.g. MCP ``isError``); the message goes to the model."""


async def run_tools(
    calls: Sequence[ToolCall], executor: ToolExecutor, timeout_seconds: float
) -> list[ToolOutcome]:
    """Execute ``calls`` concurrently. Results come back in the same order as ``calls``."""
    return list(
        await asyncio.gather(*(_run_one(call, executor, timeout_seconds) for call in calls))
    )


async def _run_one(call: ToolCall, executor: ToolExecutor, timeout_seconds: float) -> ToolOutcome:
    try:
        content = await asyncio.wait_for(executor(call), timeout_seconds)
    except TimeoutError:
        message = f"tool {call.name!r} timed out after {timeout_seconds:g}s"
        logger.warning("%s (tool_use_id=%s)", message, call.id)
        return ToolOutcome(call.id, message, is_error=True)
    except ToolExecutionError as exc:
        logger.warning("tool %r reported an error: %s", call.name, exc)
        message = str(exc)
        if len(message) > MAX_ERROR_CHARS:  # an error is not a channel for bulk content
            message = message[:MAX_ERROR_CHARS] + " [truncated]"
        return ToolOutcome(call.id, message, is_error=True)
    except Exception as exc:
        # Unexpected failure in our own executor: logged with traceback, reported to the model
        # without internals (no traceback, no connection strings).
        logger.exception("tool %r crashed (tool_use_id=%s)", call.name, call.id)
        return ToolOutcome(call.id, f"tool {call.name!r} failed: {type(exc).__name__}", True)
    return ToolOutcome(call.id, content)
