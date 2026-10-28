"""Ask the orchestrator from the terminal.

uv run python -m brigada.orchestration "Por que minhas vendas caíram na última terça?" \
    --restaurant 1 --today 2026-10-28
"""

import argparse
import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from brigada.config import get_settings
from brigada.orchestration.orchestrator import orchestrate
from brigada.orchestration.store import OrchestrationStore
from brigada.runtime.state import RunStore


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m brigada.orchestration")
    parser.add_argument("question")
    parser.add_argument("--restaurant", type=int, required=True)
    parser.add_argument("--no-save", action="store_true", help="do not persist anything")
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=datetime.now(ZoneInfo(settings.timezone)).date(),
        help="reference date for 'ontem', 'última terça'... (default: today)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    url = settings.database_url
    result = asyncio.run(
        orchestrate(
            args.question,
            args.restaurant,
            args.today,
            runs=None if args.no_save else RunStore(url),
            orchestrations=None if args.no_save else OrchestrationStore(url),
        )
    )
    if result.plan and result.plan.steps:
        print("plano:")
        for step in result.plan.steps:
            deps = f" (depois de {', '.join(step.depends_on)})" if step.depends_on else ""
            print(f"  {step.id} {step.specialist}{deps}: {step.task}")
    for i, review in enumerate(result.reviews, 1):
        verdict = "aprovada" if review.approved else "reprovada: " + "; ".join(review.issues)
        print(f"revisão {i}: {verdict}")
    print(f"\n{result.text}")
    tools = ", ".join(c.name for c in result.tools_called) or "nenhuma"
    print(
        f"\n[orquestração {result.run_id} · {result.steps} chamadas ao modelo · ferramentas: "
        f"{tools} · {result.tokens_in}+{result.tokens_out} tokens · US$ {result.cost_usd:.4f}]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
