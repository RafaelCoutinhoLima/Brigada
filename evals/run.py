"""Eval harness: run every case through an answerer, score it and write a results JSON.

Usage::

    uv run python -m evals.run                                  # default answerer (orchestrator)
    uv run python -m evals.run --answerer evals.oracle:answer   # self-test, should be 100%
    uv run python -m evals.run --subset ci --fail-under-baseline 5

Exit codes: 0 = ran (cases may still have failed; see the table), 1 = baseline gate failed,
2 = configuration error (answerer not found, baseline missing or incompatible).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import inspect
import math
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from evals.cases import CASES_PATH, load_cases
from evals.schema import CATEGORIES, AnswerResult, Case
from evals.scoring import score_case
from evals.world import END_DATE, SEED, TODAY

DEFAULT_ANSWERER = "brigada.orchestration:answer"
DEFAULT_BASELINE = Path(__file__).with_name("baseline.json")
RESULTS_DIR = Path(__file__).with_name("results")
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "src" / "brigada" / "prompts"


class Answerer(Protocol):
    async def __call__(self, question: str, restaurant_id: int, today: date) -> AnswerResult: ...


class AnswererNotFoundError(RuntimeError):
    pass


class BaselineError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------


class CaseResult(BaseModel):
    id: str
    category: str
    restaurant_id: int
    question: str
    passed: bool
    correct: bool
    expected_tools_called: bool
    forbidden_tools_called: list[str] = Field(default_factory=list)
    tools_true_positive: int = 0
    tools_called_unique: int = 0
    tools_expected_unique: int = 0
    latency_ms: float
    answer: AnswerResult | None = None
    error: str | None = None
    traceback: str | None = None


class CategoryStats(BaseModel):
    n: int
    passed: int
    accuracy: float


class Summary(BaseModel):
    n: int
    passed: int
    accuracy: float
    by_category: dict[str, CategoryStats]
    errors: int
    expected_tools_hit_rate: float
    tool_precision: float | None
    tool_recall: float | None
    forbidden_tool_violations: int
    mean_steps: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    tokens_in: int
    tokens_out: int
    cost_total_usd: float
    cost_per_question_usd: float | None


class RunMeta(BaseModel):
    answerer: str
    subset: str | None
    started_at: str
    finished_at: str
    git_commit: str | None
    cases_sha256: str
    prompts_sha256: str
    world: dict[str, str | int]
    case_ids: list[str]


class RunReport(BaseModel):
    meta: RunMeta
    summary: Summary
    cases: list[CaseResult]


# --------------------------------------------------------------------------------------
# Answerer loading
# --------------------------------------------------------------------------------------


def resolve_answerer(spec: str) -> Answerer:
    """Import ``module:attr``; raise AnswererNotFoundError with a helpful message."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise AnswererNotFoundError(f"--answerer must look like 'module:attr', got {spec!r}")
    hint = (
        f"Answerer {spec!r} is not available yet. Implement `async def {attr}(question: str, "
        f"restaurant_id: int, today: date) -> evals.schema.AnswerResult` in {module_name}, "
        "or pass e.g. --answerer evals.oracle:answer to self-test the harness."
    )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise AnswererNotFoundError(f"{hint}\n(import failed: {exc})") from exc
    fn = getattr(module, attr, None)
    if fn is None:
        raise AnswererNotFoundError(f"{hint}\n({module_name} has no attribute {attr!r})")
    if not callable(fn):
        raise AnswererNotFoundError(f"{spec!r} is not callable")
    return fn


# --------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------


async def _call(answerer: Answerer, case: Case, today: date) -> AnswerResult:
    result = answerer(case.question, case.restaurant_id, today)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, AnswerResult):
        return result
    return AnswerResult.model_validate(result)


async def run_case(answerer: Answerer, case: Case, today: date, timeout_s: float) -> CaseResult:
    start = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s):
            answer = await _call(answerer, case, today)
    except Exception as exc:  # recorded on the result and reported, never dropped
        return CaseResult(
            id=case.id,
            category=case.category,
            restaurant_id=case.restaurant_id,
            question=case.question,
            passed=False,
            correct=False,
            expected_tools_called=False,
            latency_ms=(time.perf_counter() - start) * 1000,
            error=repr(exc) if not isinstance(exc, TimeoutError) else f"timeout after {timeout_s}s",
            traceback=traceback.format_exc(),
        )
    wall_ms = (time.perf_counter() - start) * 1000
    score = score_case(case, answer)
    return CaseResult(
        id=case.id,
        category=case.category,
        restaurant_id=case.restaurant_id,
        question=case.question,
        passed=score.passed,
        correct=score.correct,
        expected_tools_called=score.expected_tools_called,
        forbidden_tools_called=score.forbidden_tools_called,
        tools_true_positive=score.tools_true_positive,
        tools_called_unique=score.tools_called_unique,
        tools_expected_unique=score.tools_expected_unique,
        latency_ms=answer.latency_ms if answer.latency_ms is not None else wall_ms,
        answer=answer,
    )


async def run_cases(
    cases: Sequence[Case],
    answerer: Answerer,
    *,
    concurrency: int = 4,
    timeout_s: float = 180.0,
    today: date = TODAY,
) -> list[CaseResult]:
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    sem = asyncio.Semaphore(concurrency)

    async def bounded(case: Case) -> CaseResult:
        async with sem:
            return await run_case(answerer, case, today, timeout_s)

    return list(await asyncio.gather(*(bounded(c) for c in cases)))


# --------------------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in [0, 100]. None for an empty sequence."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def summarize(results: Sequence[CaseResult]) -> Summary:
    n = len(results)
    if n == 0:
        raise ValueError("no results to summarize")
    by_category: dict[str, CategoryStats] = {}
    for cat in [*CATEGORIES, *sorted({r.category for r in results} - set(CATEGORIES))]:
        rs = [r for r in results if r.category == cat]
        if rs:
            passed = sum(r.passed for r in rs)
            by_category[cat] = CategoryStats(n=len(rs), passed=passed, accuracy=passed / len(rs))

    answered = [r for r in results if r.answer is not None]
    answers = [r.answer for r in answered if r.answer is not None]
    # Tool precision/recall: set-based per case, micro-averaged. Precision only over cases that
    # called at least one tool; recall only over cases that expect at least one tool. Errored
    # cases count as "called nothing".
    tp_called = sum(r.tools_true_positive for r in results if r.tools_called_unique)
    called = sum(r.tools_called_unique for r in results)
    tp_expected = sum(r.tools_true_positive for r in results if r.tools_expected_unique)
    expected = sum(r.tools_expected_unique for r in results)
    with_expected = [r for r in results if r.tools_expected_unique]
    cost = sum(a.cost_usd for a in answers)
    passed = sum(r.passed for r in results)
    return Summary(
        n=n,
        passed=passed,
        accuracy=passed / n,
        by_category=by_category,
        errors=sum(r.error is not None for r in results),
        expected_tools_hit_rate=(
            sum(r.expected_tools_called for r in with_expected) / len(with_expected)
            if with_expected
            else 1.0
        ),
        tool_precision=_ratio(tp_called, called),
        tool_recall=_ratio(tp_expected, expected),
        forbidden_tool_violations=sum(bool(r.forbidden_tools_called) for r in results),
        mean_steps=sum(a.steps for a in answers) / len(answers) if answers else None,
        latency_p50_ms=percentile([r.latency_ms for r in answered], 50),
        latency_p95_ms=percentile([r.latency_ms for r in answered], 95),
        tokens_in=sum(a.tokens_in for a in answers),
        tokens_out=sum(a.tokens_out for a in answers),
        cost_total_usd=cost,
        cost_per_question_usd=cost / len(answers) if answers else None,
    )


# --------------------------------------------------------------------------------------
# Baseline gate
# --------------------------------------------------------------------------------------


class GateResult(BaseModel):
    ok: bool
    drop_points: float
    message: str


def check_baseline(current: RunReport, baseline: RunReport, max_drop_points: float) -> GateResult:
    """Fail when accuracy is more than ``max_drop_points`` percentage points below baseline."""
    if current.meta.subset != baseline.meta.subset:
        raise BaselineError(
            f"baseline subset {baseline.meta.subset!r} != current subset "
            f"{current.meta.subset!r}; compare runs over the same cases"
        )
    drop = round((baseline.summary.accuracy - current.summary.accuracy) * 100, 6)
    ok = drop <= max_drop_points
    verdict = "OK" if ok else "FAIL"
    message = (
        f"{verdict}: accuracy {current.summary.accuracy:.1%} vs baseline "
        f"{baseline.summary.accuracy:.1%} (drop {drop:+.1f} pts, allowed {max_drop_points} pts)"
    )
    if set(current.meta.case_ids) != set(baseline.meta.case_ids):
        message += " [warning: case ids differ from the baseline; consider refreshing it]"
    return GateResult(ok=ok, drop_points=drop, message=message)


def load_report(path: Path) -> RunReport:
    if not path.exists():
        raise BaselineError(
            f"baseline {path} not found. Create it from a trusted run over the same subset, "
            "e.g. `cp evals/results/<run>.json evals/baseline.json`"
        )
    return RunReport.model_validate_json(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# Metadata, output
# --------------------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prompts_hash(prompts_dir: Path = PROMPTS_DIR) -> str:
    """Hash of every file under src/brigada/prompts (paths + contents), for run provenance."""
    digest = hashlib.sha256()
    if prompts_dir.is_dir():
        for p in sorted(x for x in prompts_dir.rglob("*") if x.is_file()):
            digest.update(p.relative_to(prompts_dir).as_posix().encode())
            digest.update(b"\0")
            digest.update(p.read_bytes())
    return digest.hexdigest()


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def default_out_path(answerer: str, subset: str | None, now: datetime) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", answerer).strip("-").lower()
    suffix = f"-{subset}" if subset else ""
    return RESULTS_DIR / f"{now:%Y%m%d-%H%M%S}-{slug}{suffix}.json"


def _fmt(value: float | None, spec: str, suffix: str = "") -> str:
    return "–" if value is None else f"{value:{spec}}{suffix}"


def format_table(report: RunReport) -> str:
    s = report.summary
    lines = [
        f"Answerer: {report.meta.answerer}   subset: {report.meta.subset or 'all'}   n={s.n}",
        "",
        f"{'category':<14} {'n':>3} {'passed':>6} {'accuracy':>9}",
        "-" * 35,
    ]
    for cat, st in s.by_category.items():
        lines.append(f"{cat:<14} {st.n:>3} {st.passed:>6} {st.accuracy:>9.1%}")
    lines += [
        "-" * 35,
        f"{'overall':<14} {s.n:>3} {s.passed:>6} {s.accuracy:>9.1%}",
        "",
        f"errors:               {s.errors}",
        f"expected tools hit:   {s.expected_tools_hit_rate:.1%}",
        f"tool precision:       {_fmt(s.tool_precision, '.1%')}",
        f"tool recall:          {_fmt(s.tool_recall, '.1%')}",
        f"forbidden tool calls: {s.forbidden_tool_violations}",
        f"mean steps:           {_fmt(s.mean_steps, '.2f')}",
        f"latency p50 / p95:    {_fmt(s.latency_p50_ms, '.0f', ' ms')} / "
        f"{_fmt(s.latency_p95_ms, '.0f', ' ms')}",
        f"tokens in / out:      {s.tokens_in} / {s.tokens_out}",
        f"cost total / per q:   ${s.cost_total_usd:.4f} / {_fmt(s.cost_per_question_usd, '.4f')}",
    ]
    failed = [r for r in report.cases if not r.passed]
    if failed:
        lines += ["", "Failed cases:"]
        for r in failed:
            if r.error:
                why = f"ERROR {r.error}"
            elif r.forbidden_tools_called:
                why = f"forbidden tools called: {', '.join(r.forbidden_tools_called)}"
            else:
                text = r.answer.text if r.answer else ""
                why = f"wrong answer: {text[:80]!r}"
            lines.append(f"  {r.id:<12} {why}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


async def evaluate(
    answerer_spec: str,
    *,
    subset: str | None = None,
    cases_path: Path = CASES_PATH,
    concurrency: int = 4,
    timeout_s: float = 180.0,
    answerer: Answerer | None = None,
) -> RunReport:
    """Load cases, run them and build the report (no file I/O besides reading cases)."""
    cases = load_cases(cases_path, subset=subset)
    fn = answerer if answerer is not None else resolve_answerer(answerer_spec)
    started = datetime.now(UTC)
    results = await run_cases(cases, fn, concurrency=concurrency, timeout_s=timeout_s)
    return RunReport(
        meta=RunMeta(
            answerer=answerer_spec,
            subset=subset,
            started_at=started.isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            git_commit=git_commit(),
            cases_sha256=_sha256_file(cases_path),
            prompts_sha256=prompts_hash(),
            world={"seed": SEED, "end_date": END_DATE.isoformat(), "today": TODAY.isoformat()},
            case_ids=[c.id for c in cases],
        ),
        summary=summarize(results),
        cases=results,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the Brigada eval suite.")
    p.add_argument("--subset", default=None, help="only cases tagged with this subset (e.g. ci)")
    p.add_argument("--answerer", default=DEFAULT_ANSWERER, help="module:attr of the answerer")
    p.add_argument("--cases", type=Path, default=CASES_PATH)
    p.add_argument("--out", type=Path, default=None, help="results JSON (default evals/results/)")
    p.add_argument("--baseline", type=Path, default=None, help=f"default {DEFAULT_BASELINE}")
    p.add_argument(
        "--fail-under-baseline",
        type=float,
        default=None,
        metavar="N",
        help="exit 1 if accuracy is more than N points below the baseline",
    )
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--timeout", type=float, default=180.0, help="per-case timeout in seconds")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        answerer = resolve_answerer(args.answerer)
    except AnswererNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    baseline: RunReport | None = None
    if args.fail_under_baseline is not None or args.baseline is not None:
        try:
            baseline = load_report(args.baseline or DEFAULT_BASELINE)
        except BaselineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    report = asyncio.run(
        evaluate(
            args.answerer,
            subset=args.subset,
            cases_path=args.cases,
            concurrency=args.concurrency,
            timeout_s=args.timeout,
            answerer=answerer,
        )
    )
    out: Path = args.out or default_out_path(args.answerer, args.subset, datetime.now())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    print(format_table(report))
    for r in report.cases:
        if r.error:
            print(f"\n[{r.id}] {r.error}\n{r.traceback}", file=sys.stderr)
    print(f"\nresults: {out}")

    if baseline is not None:
        try:
            gate = check_baseline(report, baseline, args.fail_under_baseline or 0.0)
        except BaselineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.fail_under_baseline is None:
            print(
                f"baseline: drop {gate.drop_points:+.1f} pts (no gate; pass --fail-under-baseline)"
            )
        else:
            print(gate.message)
            if not gate.ok:
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
