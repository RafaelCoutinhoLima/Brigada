"""The Sales specialist: the first concrete agent, used to prove the runtime end to end.

Kept as a thin entry point over ``brigada.runtime.specialists``.
"""

from datetime import date

from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer

from brigada.runtime.agent import ModelClient
from brigada.runtime.specialists import SALES, ask, resume, system_prompt
from brigada.runtime.state import RunStore
from brigada.runtime.types import RunResult

SALES_TOOLS = SALES.tools


def sales_system_prompt(restaurant_id: int, today: date) -> str:
    return system_prompt(SALES, restaurant_id, today)


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
    return await ask(
        SALES,
        question,
        restaurant_id,
        today,
        client=client,
        server=server,
        run_id=run_id,
        store=store,
    )


async def resume_sales(
    run_id: str,
    store: RunStore,
    *,
    client: ModelClient | None = None,
    server: MCPServer | StdioServerParameters | None = None,
) -> RunResult:
    return await resume(run_id, store, client=client, server=server, expected=SALES)
