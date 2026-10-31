"""Rewrite the Results table in README.md from an eval results JSON.

Only the text between ``<!-- results:start -->`` and ``<!-- results:end -->`` is replaced.

Usage::

    uv run python scripts/update_readme_results.py evals/results/<run>.json [--readme README.md]

The router row comes from ``evals/router_metrics.json`` (``python -m brigada.router.train``)
when that file exists, and is "–" otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

START = "<!-- results:start -->"
END = "<!-- results:end -->"
DASH = "–"


def _pct(value: float | None) -> str:
    return DASH if value is None else f"{value * 100:.1f}%"


def render_table(results: dict[str, Any], router_row: str | None = None) -> str:
    s = results["summary"]
    meta = results["meta"]
    subset = meta.get("subset") or "all"
    p50 = s.get("latency_p50_ms")
    p95 = s.get("latency_p95_ms")
    cost = s.get("cost_per_question_usd")
    steps = s.get("mean_steps")
    rows = [
        ("Eval accuracy", f"{_pct(s['accuracy'])} ({s['passed']}/{s['n']}, subset: {subset})"),
        (
            "Tool precision / recall",
            f"{_pct(s.get('tool_precision'))} / {_pct(s.get('tool_recall'))}",
        ),
        ("Mean steps", DASH if steps is None else f"{steps:.2f}"),
        ("p50 latency", DASH if p50 is None else f"{p50 / 1000:.1f} s"),
        ("p95 latency", DASH if p95 is None else f"{p95 / 1000:.1f} s"),
        ("Cost per question", DASH if cost is None else f"US$ {cost:.4f}"),
        ("Router savings vs always-strong", router_row or DASH),
    ]
    lines = ["| Metric | Value |", "|--------|-------|"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    commit = (meta.get("git_commit") or "")[:7] or "unknown"
    lines += [
        "",
        f"_Answerer `{meta['answerer']}`, commit `{commit}`, {meta['finished_at'][:10]}._",
    ]
    return "\n".join(lines)


def replace_between_markers(readme: str, block: str) -> str:
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError(f"README must contain exactly one {START} and one {END}")
    if readme.index(START) > readme.index(END):
        raise ValueError(f"{START} must come before {END}")
    head, rest = readme.split(START)
    _, tail = rest.split(END)
    return f"{head}{START}\n{block}\n{END}{tail}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", type=Path, help="results JSON written by evals.run")
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--router-metrics", type=Path, default=Path("evals/router_metrics.json"))
    args = parser.parse_args(argv)
    results = json.loads(args.results.read_text(encoding="utf-8"))
    router_row = None
    if args.router_metrics.is_file():
        router_row = json.loads(args.router_metrics.read_text(encoding="utf-8"))["readme_row"]
    readme = args.readme.read_text(encoding="utf-8")
    try:
        updated = replace_between_markers(readme, render_table(results, router_row))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.readme.write_text(updated, encoding="utf-8")
    print(f"updated results table in {args.readme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
