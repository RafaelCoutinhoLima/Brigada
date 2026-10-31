"""Build ``evals/router_labels.jsonl`` from two live eval results: one per specialist model.

Each line is one question with whether the fast and the strong model answered it correctly,
and the tokens each run spent on it. The result JSONs stay out of git (``evals/results/`` is
ignored); this JSONL is what the router is trained on.

Usage::

    uv run python scripts/build_router_labels.py \\
        --strong evals/results/live-full-v3.json --fast evals/results/live-fast-v1.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

FIELDS = (
    "id",
    "question",
    "category",
    "fast_passed",
    "strong_passed",
    "fast_tokens_in",
    "fast_tokens_out",
    "strong_tokens_in",
    "strong_tokens_out",
)


def _by_id(results: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for case in results["cases"]:
        if case["id"] in cases:
            raise ValueError(f"{label}: case {case['id']!r} appears twice")
        cases[case["id"]] = case
    return cases


def _tokens(case: dict[str, Any] | None, key: str) -> int | None:
    answer = (case or {}).get("answer") or {}
    value = answer.get(key)
    return None if value is None else int(value)


def build_rows(strong: dict[str, Any], fast: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per case id found in either run, in the fast run's order, then the rest.

    A case missing from one run gets ``None`` for that run's label and tokens. The same id
    with a different question or category in the two runs is an error, not a guess.
    """
    s, f = _by_id(strong, "strong"), _by_id(fast, "fast")
    ids = list(f) + [i for i in s if i not in f]
    rows: list[dict[str, Any]] = []
    for case_id in ids:
        sc, fc = s.get(case_id), f.get(case_id)
        if sc and fc and (sc["question"], sc["category"]) != (fc["question"], fc["category"]):
            raise ValueError(f"case {case_id!r} differs between the two runs")
        ref = fc or sc
        assert ref is not None
        rows.append(
            {
                "id": case_id,
                "question": ref["question"],
                "category": ref["category"],
                "fast_passed": None if fc is None else bool(fc["passed"]),
                "strong_passed": None if sc is None else bool(sc["passed"]),
                "fast_tokens_in": _tokens(fc, "tokens_in"),
                "fast_tokens_out": _tokens(fc, "tokens_out"),
                "strong_tokens_in": _tokens(sc, "tokens_in"),
                "strong_tokens_out": _tokens(sc, "tokens_out"),
            }
        )
    return rows


def render(rows: list[dict[str, Any]]) -> str:
    """Deterministic JSONL: fixed key order, UTF-8 kept, one trailing newline."""
    return "".join(
        json.dumps({k: row[k] for k in FIELDS}, ensure_ascii=False) + "\n" for row in rows
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strong", type=Path, default=Path("evals/results/live-full-v3.json"))
    parser.add_argument("--fast", type=Path, default=Path("evals/results/live-fast-v1.json"))
    parser.add_argument("--out", type=Path, default=Path("evals/router_labels.jsonl"))
    args = parser.parse_args(argv)
    strong = json.loads(args.strong.read_text(encoding="utf-8"))
    fast = json.loads(args.fast.read_text(encoding="utf-8"))
    try:
        rows = build_rows(strong, fast)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(render(rows), encoding="utf-8")
    both = sum(1 for r in rows if r["fast_passed"] is not None and r["strong_passed"] is not None)
    print(f"wrote {len(rows)} rows ({both} with both labels) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
