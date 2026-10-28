"""Ask the Sales agent from the terminal.

uv run python -m brigada.runtime "Quanto vendi ontem?" --restaurant 1 --today 2026-10-28
"""

import argparse
import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from brigada.config import get_settings
from brigada.runtime.sales import ask_sales


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m brigada.runtime")
    parser.add_argument("question")
    parser.add_argument("--restaurant", type=int, required=True)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=datetime.now(ZoneInfo(settings.timezone)).date(),
        help="reference date for 'ontem', 'última terça'... (default: today)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    result = asyncio.run(ask_sales(args.question, args.restaurant, args.today))
    print(result.text)
    tools = ", ".join(c.name for c in result.tools_called) or "nenhuma"
    print(
        f"\n[{result.steps} passos · ferramentas: {tools} · "
        f"{result.tokens_in}+{result.tokens_out} tokens · US$ {result.cost_usd:.4f}]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
