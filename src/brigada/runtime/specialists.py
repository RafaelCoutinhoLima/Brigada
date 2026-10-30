"""The specialist agents the orchestrator delegates to.

Each specialist is the same runtime loop with its own versioned prompt and its own slice of the
MCP tools (least privilege): only Menu sees the write tools, and those only propose changes.
"""

import uuid
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Specialist:
    name: str
    tools: tuple[str, ...]
    description: (
        str  # what the planner reads to decide who gets each step; the prompt file is <name>.md
    )


SALES = Specialist(
    name="sales",
    tools=("sales_by_period", "top_items", "list_menu_items"),
    description="Faturamento e pedidos por período, dia, canal, hora ou dia da semana; "
    "itens mais vendidos.",
)
MENU = Specialist(
    name="menu",
    tools=(
        "list_menu_items",
        "top_items",
        "ratings_by_item",
        "reviews_for_item",
        "search_reviews",
        "pause_menu_item",
        "create_promotion",
    ),
    description="Cardápio, avaliações e reclamações de clientes por prato; propõe pausar "
    "pratos ou criar promoções (sempre pendentes de aprovação humana).",
)
OPERATIONS = Specialist(
    name="operations",
    tools=("delivery_times", "sales_by_period"),
    description="Tempos de entrega prometido vs real por dia da semana e hora; volume de "
    "pedidos por hora e canal para entender a carga da operação.",
)

INVENTORY = Specialist(
    name="inventory",
    tools=(
        "stock_levels",
        "ingredient_consumption",
        "dish_margins",
        "cmv",
        "waste_report",
        "cost_changes",
        "propose_purchase",
    ),
    description="Estoque e custos: o que vai faltar e o que comprar, consumo de insumos, "
    "margem e lucro por prato, CMV, desperdício e insumos que ficaram mais caros; propõe "
    "compras (sempre pendentes de aprovação humana).",
)

SPECIALISTS: dict[str, Specialist] = {s.name: s for s in (SALES, MENU, OPERATIONS, INVENTORY)}

_WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def get_specialist(name: str) -> Specialist:
    try:
        return SPECIALISTS[name]
    except KeyError:
        raise ValueError(f"unknown specialist {name!r}; known: {sorted(SPECIALISTS)}") from None


def system_prompt(specialist: Specialist, restaurant_id: int, today: date) -> str:
    # Run context goes at the end so the stable instructions stay a cacheable prefix.
    return (
        f"{load_prompt(specialist.name).text}\n"
        f"Contexto desta conversa:\n"
        f"- restaurant_id: {restaurant_id}\n"
        f"- hoje: {today.isoformat()} ({_WEEKDAYS[today.weekday()]})\n"
    )


async def ask(
    specialist: Specialist,
    question: str,
    restaurant_id: int,
    today: date,
    *,
    client: ModelClient | None = None,
    server: MCPServer | StdioServerParameters | None = None,
    run_id: str | None = None,
    store: RunStore | None = None,
    tainted: bool = False,
    orchestration_id: str | None = None,
    plan_step: str | None = None,
) -> RunResult:
    """Answer one question. With a ``store``, every step is saved and the run is resumable.

    ``tainted`` starts the run with write tools disabled: the question carries text derived
    from customer reviews (e.g. another specialist's findings), see ``mcp_tools``.
    """
    run_id = run_id or uuid.uuid4().hex
    async with mcp_toolbox(
        run_id, restaurant_id, specialist.tools, server, tainted=tainted
    ) as toolbox:
        agent = _agent(specialist, restaurant_id, today, client, toolbox.tools, toolbox.executor)
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
            prompt_sha256=load_prompt(specialist.name).sha256,
            orchestration_id=orchestration_id,
            plan_step=plan_step,
            tainted=tainted,
        )


async def resume(
    run_id: str,
    store: RunStore,
    *,
    client: ModelClient | None = None,
    server: MCPServer | StdioServerParameters | None = None,
    expected: Specialist | None = None,
) -> RunResult:
    """Continue a failed run with the specialist that started it."""
    record = await store.load(run_id)
    if expected is not None and record.agent != expected.name:
        raise ValueError(f"run {run_id!r} belongs to agent {record.agent!r}, not {expected.name!r}")
    specialist = get_specialist(record.agent)
    tainted = record.tainted or read_untrusted(record.messages)
    async with mcp_toolbox(
        run_id, record.restaurant_id, specialist.tools, server, tainted=tainted
    ) as toolbox:
        agent = _agent(
            specialist, record.restaurant_id, record.today, client, toolbox.tools, toolbox.executor
        )
        return await resume_persisted(
            agent, store, record, prompt_sha256=load_prompt(specialist.name).sha256
        )


def _agent(
    specialist: Specialist,
    restaurant_id: int,
    today: date,
    client: ModelClient | None,
    tools: list[ToolParam],
    executor: ToolExecutor,
) -> Agent:
    settings = get_settings()
    return Agent(
        AgentConfig(
            name=specialist.name,
            model=settings.model_strong,
            system=system_prompt(specialist, restaurant_id, today),
            max_steps=settings.max_agent_steps,
            tool_timeout_seconds=settings.tool_timeout_seconds,
        ),
        client or make_client(),
        tools,
        executor,
    )
