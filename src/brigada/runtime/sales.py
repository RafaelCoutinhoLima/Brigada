"""The Sales specialist: the first concrete agent, used to prove the runtime end to end."""

import uuid
from datetime import date

from anthropic.types import ToolParam
from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer

from brigada.config import get_settings
from brigada.prompts import load_prompt
from brigada.runtime.agent import Agent, AgentConfig, ModelClient, make_client
from brigada.runtime.mcp_tools import mcp_toolbox, read_untrusted
from brigada.runtime.state import RunStore, resume_persisted, run_persisted
from brigada.runtime.tools import ToolExecutor
from brigada.runtime.types import RunResult

SALES_TOOLS = ("sales_by_period", "top_items", "list_menu_items")


def sales_system_prompt(restaurant_id: int, today: date) -> str:
    # Run context goes at the end so the stable instructions stay a cacheable prefix.
    return (
        f"{load_prompt('sales').text}\n"
        f"Contexto desta conversa:\n"
        f"- restaurant_id: {restaurant_id}\n"
        f"- hoje: {today.isoformat()} ({_WEEKDAYS[today.weekday()]})\n"
    )


_WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


async def ask_sales(
    question: str,
    restaurant_id: int,
    today: date,
    *,
    client: ModelClient | None = None,
    server: MCPServer | StdioServerParameters | None = None,
    run_id: str | None = None,
    store: RunStore | None = None,
) -> RunResult:
    """Answer one question. With a ``store``, every step is saved and the run is resumable."""
    run_id = run_id or uuid.uuid4().hex
    async with mcp_toolbox(run_id, restaurant_id, SALES_TOOLS, server) as toolbox:
        agent = _sales_agent(restaurant_id, today, client, toolbox.tools, toolbox.executor)
        if store is None:
            result = await agent.run(question)
            result.run_id = run_id
            return result
        return await run_persisted(
            agent,
            store,
            run_id,
            restaurant_id=restaurant_id,
            question=question,
            today=today,
            prompt_sha256=load_prompt("sales").sha256,
        )


async def resume_sales(
    run_id: str,
    store: RunStore,
    *,
    client: ModelClient | None = None,
    server: MCPServer | StdioServerParameters | None = None,
) -> RunResult:
    record = await store.load(run_id)
    if record.agent != "sales":
        raise ValueError(f"run {run_id!r} belongs to agent {record.agent!r}, not 'sales'")
    tainted = read_untrusted(record.messages)
    async with mcp_toolbox(
        run_id, record.restaurant_id, SALES_TOOLS, server, tainted=tainted
    ) as toolbox:
        agent = _sales_agent(
            record.restaurant_id, record.today, client, toolbox.tools, toolbox.executor
        )
        return await resume_persisted(
            agent, store, record, prompt_sha256=load_prompt("sales").sha256
        )


def _sales_agent(
    restaurant_id: int,
    today: date,
    client: ModelClient | None,
    tools: list[ToolParam],
    executor: ToolExecutor,
) -> Agent:
    settings = get_settings()
    return Agent(
        AgentConfig(
            name="sales",
            model=settings.model_strong,
            system=sales_system_prompt(restaurant_id, today),
            max_steps=settings.max_agent_steps,
            tool_timeout_seconds=settings.tool_timeout_seconds,
        ),
        client or make_client(settings.anthropic_api_key or None),
        tools,
        executor,
    )
