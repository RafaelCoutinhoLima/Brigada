"""Loading eval cases from JSONL."""

from __future__ import annotations

from pathlib import Path

from evals.schema import Case

CASES_PATH = Path(__file__).with_name("cases.jsonl")


def load_cases(path: Path = CASES_PATH, subset: str | None = None) -> list[Case]:
    """Parse and validate every line; a bad line raises with its line number."""
    cases: list[Case] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(Case.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: invalid case: {exc}") from exc
    if subset is not None:
        cases = [c for c in cases if subset in c.subsets]
        if not cases:
            raise ValueError(f"subset {subset!r} has no cases in {path}")
    return cases
