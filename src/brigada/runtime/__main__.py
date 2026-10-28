"""Ask a specialist agent from the terminal.

uv run python -m brigada.runtime "Quanto vendi ontem?" --restaurant 1 --today 2026-10-28
uv run python -m brigada.runtime "Qual prato tem mais reclamações?" --agent menu --restaurant 2
"""

import argparse
import asyncio
import logging
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from brigada.config import get_settings
from brigada.runtime.specialists import SPECIALISTS, ask, get_specialist, resume
from brigada.runtime.state import RunStore


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m brigada.runtime")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--restaurant", type=int)
    parser.add_argument("--agent", choices=sorted(SPECIALISTS), default="sales")
    parser.add_argument("--resume", metavar="RUN_ID", help="continue a failed run")
    parser.add_argument("--no-save", action="store_true", help="do not persist the run")
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=datetime.now(ZoneInfo(settings.timezone)).date(),
        help="reference date for 'ontem', 'última terça'... (default: today)",
    )
    args = parser.parse_args(argv)
    if not args.resume and (args.question is None or args.restaurant is None):
        parser.error("a question and --restaurant are required (or --resume RUN_ID)")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    store = None if args.no_save else RunStore(settings.database_url)
    if args.resume:
        if store is None:
            parser.error("--resume needs the database (drop --no-save)")
        result = asyncio.run(resume(args.resume, store))
    else:
        run_id = uuid.uuid4().hex
        print(f"run_id: {run_id}")
        result = asyncio.run(
            ask(
                get_specialist(args.agent),
                args.question,
                args.restaurant,
                args.today,
                run_id=run_id,
                store=store,
            )
        )
    print(result.text)
    tools = ", ".join(c.name for c in result.tools_called) or "nenhuma"
    print(
        f"\n[{result.steps} passos · ferramentas: {tools} · "
        f"{result.tokens_in}+{result.tokens_out} tokens · US$ {result.cost_usd:.4f}]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
