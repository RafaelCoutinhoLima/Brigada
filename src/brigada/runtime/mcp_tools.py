"""Bridge between the agent loop and the Brigada MCP server.

Opens an MCP client, turns the server's tool list into Claude ``tools`` definitions and gives
the loop a ``ToolExecutor`` that forwards calls with the run's scope in ``_meta``
(``brigada/run_id`` for the audit log, ``brigada/restaurant_id`` for tenant isolation).
"""

import json
import os
import sys
from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from anthropic.types import MessageParam, ToolParam
from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment
from mcp.server.mcpserver import MCPServer
from mcp_types import TextContent

from brigada.config import get_settings
from brigada.mcp_server.audit import (
    RESTAURANT_ID_META_KEY,
    RUN_ID_META_KEY,
    TAINTED_META_KEY,
    TOOL_USE_ID_META_KEY,
)
from brigada.runtime.tools import ToolExecutionError, ToolExecutor
from brigada.runtime.types import ToolCall

# Tools whose output is customer-written text (possible prompt injection), and tools that
# propose changes. Once a run has read untrusted text, it cannot propose changes anymore.
UNTRUSTED_OUTPUT_TOOLS = frozenset({"reviews_for_item"})
WRITE_TOOLS = frozenset({"pause_menu_item", "create_promotion"})


def read_untrusted(messages: Sequence[MessageParam]) -> bool:
    """True if the conversation already called a tool that returns untrusted text."""
    for m in messages:
        if m["role"] != "assistant" or isinstance(m["content"], str):
            continue
        for b in m["content"]:
            block = b if isinstance(b, dict) else b.to_dict()  # type: ignore[union-attr]
            if block.get("type") == "tool_use" and block.get("name") in UNTRUSTED_OUTPUT_TOOLS:
                return True
    return False


@dataclass(frozen=True)
class Toolbox:
    tools: list[ToolParam]
    executor: ToolExecutor


def stdio_server() -> StdioServerParameters:
    """Run the MCP server as a child process. Only the variables it needs are passed on:
    the default safe set plus DATABASE_URL (never ANTHROPIC_API_KEY)."""
    env = get_default_environment()
    env["DATABASE_URL"] = os.environ.get("DATABASE_URL", get_settings().database_url)
    env["TIMEZONE"] = get_settings().timezone  # parent and child must agree on "today"
    env["BRIGADA_NO_DOTENV"] = "1"
    return StdioServerParameters(command=sys.executable, args=["-m", "brigada.mcp_server"], env=env)


@asynccontextmanager
async def mcp_toolbox(
    run_id: str,
    restaurant_id: int,
    allowed_tools: Collection[str] | None = None,
    server: MCPServer | StdioServerParameters | None = None,
    *,
    tainted: bool = False,
) -> AsyncIterator[Toolbox]:
    """Connect to the MCP server for the duration of one run.

    ``allowed_tools`` limits what this agent sees *and* can call: a sales specialist gets no
    write tools, so a prompt injection cannot make it pause a dish. ``tainted`` says the run
    already read untrusted text (on resume, see ``read_untrusted``).
    """
    try:
        async with Client(server or stdio_server()) as client:
            async with _toolbox(client, run_id, restaurant_id, allowed_tools, tainted) as box:
                yield box
    except BaseExceptionGroup as group:
        # The MCP client runs a TaskGroup, so anything raised inside this block (including the
        # agent's own MaxStepsExceeded, ModelRefused...) comes out wrapped. Unwrap a single
        # error so callers can `except AgentError` as usual.
        raise _sole_exception(group) from None


@asynccontextmanager
async def _toolbox(
    client: Client,
    run_id: str,
    restaurant_id: int,
    allowed_tools: Collection[str] | None,
    tainted: bool,
) -> AsyncIterator[Toolbox]:
    listed = (await client.list_tools()).tools
    known = {t.name for t in listed}
    if allowed_tools is not None and (missing := set(allowed_tools) - known):
        raise ValueError(f"MCP server does not expose tools: {sorted(missing)}")
    visible = [t for t in listed if allowed_tools is None or t.name in allowed_tools]
    names = {t.name for t in visible}
    meta = {RUN_ID_META_KEY: run_id, RESTAURANT_ID_META_KEY: restaurant_id}
    state = {"tainted": tainted}

    async def execute(call: ToolCall) -> str:
        if call.name not in names:
            raise ToolExecutionError(f"tool {call.name!r} is not available to this agent")
        # The server refuses writes from a tainted run and audits the attempt.
        call_meta = {**meta, TOOL_USE_ID_META_KEY: call.id, TAINTED_META_KEY: state["tainted"]}
        if call.name in UNTRUSTED_OUTPUT_TOOLS:
            state["tainted"] = True  # set before the call: a failed read may still carry text
        result = await client.call_tool(call.name, call.input, meta=call_meta)
        text = _result_text(result.structured_content, result.content)
        if result.is_error:
            raise ToolExecutionError(text)
        return text

    yield Toolbox(
        tools=[
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.input_schema,
            }
            for t in visible
        ],
        executor=execute,
    )


def _sole_exception(group: BaseExceptionGroup) -> BaseException:
    exc: BaseException = group
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _result_text(structured: dict[str, Any] | None, content: list[Any]) -> str:
    """Structured JSON when the tool returns it (stable to parse), else the text blocks."""
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, default=str)
    return "\n".join(c.text for c in content if isinstance(c, TextContent))
