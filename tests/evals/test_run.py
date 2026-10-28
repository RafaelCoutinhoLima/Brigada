"""The harness, exercised end to end with the oracle, the dummy and failing answerers."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from evals import dummy, oracle
from evals.cases import load_cases
from evals.oracle import format_ptbr
from evals.run import (
    AnswererNotFoundError,
    BaselineError,
    RunReport,
    check_baseline,
    evaluate,
    load_report,
    main,
    percentile,
    resolve_answerer,
    run_cases,
)
from evals.schema import AnswerResult
from evals.world import TODAY


async def test_oracle_scores_100_percent() -> None:
    report = await evaluate("evals.oracle:answer")
    s = report.summary
    assert s.n == 50
    assert s.accuracy == 1.0, [c.id for c in report.cases if not c.passed]
    assert s.errors == 0
    assert s.tool_precision == 1.0 and s.tool_recall == 1.0
    assert s.expected_tools_hit_rate == 1.0
    assert all(st.accuracy == 1.0 for st in s.by_category.values())


@pytest.mark.parametrize("subset", [None, "ci"])
async def test_dummy_scores_low(subset: str | None) -> None:
    report = await evaluate("evals.dummy:answer", subset=subset)
    assert report.summary.accuracy < 0.30
    assert report.summary.errors == 0


async def test_exception_counts_as_incorrect_and_is_reported() -> None:
    cases = load_cases(subset="ci")

    async def flaky(question: str, restaurant_id: int, today: date) -> AnswerResult:
        if question == cases[0].question:
            raise RuntimeError("tool exploded")
        return await oracle.answer(question, restaurant_id, today)

    results = await run_cases(cases, flaky)
    failed = [r for r in results if r.error]
    assert [r.id for r in failed] == [cases[0].id]
    assert not failed[0].passed and not failed[0].correct
    assert "tool exploded" in (failed[0].error or "")
    assert "RuntimeError" in (failed[0].traceback or "")
    assert sum(r.passed for r in results) == len(cases) - 1


async def test_timeout_and_bad_return_type_are_errors() -> None:
    cases = load_cases(subset="ci")[:2]

    async def slow(question: str, restaurant_id: int, today: date) -> AnswerResult:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    results = await run_cases(cases, slow, timeout_s=0.01)
    assert all(r.error and "timeout" in r.error for r in results)

    async def wrong(question: str, restaurant_id: int, today: date) -> AnswerResult:
        return 42  # type: ignore[return-value]

    results = await run_cases(cases, wrong)
    assert all(r.error and not r.passed for r in results)


async def test_concurrency_is_bounded_and_today_is_passed() -> None:
    cases = load_cases()
    active = peak = 0
    seen_today: set[date] = set()

    async def tracking(question: str, restaurant_id: int, today: date) -> AnswerResult:
        nonlocal active, peak
        seen_today.add(today)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return await dummy.answer(question, restaurant_id, today)

    await run_cases(cases, tracking, concurrency=3)
    assert peak == 3
    assert seen_today == {TODAY} == {date(2026, 10, 28)}


def test_resolve_answerer() -> None:
    assert resolve_answerer("evals.oracle:answer") is oracle.answer
    with pytest.raises(AnswererNotFoundError, match="not available yet"):
        resolve_answerer("evals.oracle:nope")
    with pytest.raises(AnswererNotFoundError, match="import failed"):
        resolve_answerer("evals.does_not_exist:answer")
    with pytest.raises(AnswererNotFoundError, match="module:attr"):
        resolve_answerer("evals.oracle")


def test_percentile() -> None:
    assert percentile([], 50) is None
    assert percentile([5.0], 95) == 5.0
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(list(range(1, 101)), 95) == pytest.approx(95.05)


def test_format_ptbr() -> None:
    assert format_ptbr(1234.5) == "1.234,50"
    assert format_ptbr(39.6, 1) == "39,6"
    assert format_ptbr(16, 0) == "16"


# --- baseline gate --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ci_report() -> RunReport:
    return asyncio.run(evaluate("evals.oracle:answer", subset="ci"))


def _with_accuracy(report: RunReport, accuracy: float, subset: str | None = "ci") -> RunReport:
    return report.model_copy(
        update={
            "summary": report.summary.model_copy(update={"accuracy": accuracy}),
            "meta": report.meta.model_copy(update={"subset": subset}),
        }
    )


@pytest.mark.parametrize(
    ("baseline_acc", "current_acc", "max_drop", "ok"),
    [
        (0.80, 0.80, 5, True),
        (0.80, 0.75, 5, True),  # exactly 5 points: allowed
        (0.80, 0.7499, 5, False),  # just over 5 points
        (0.80, 0.60, 5, False),
        (0.60, 0.90, 5, True),  # improvements always pass
        (0.80, 0.79, 0, False),
    ],
)
def test_check_baseline_boundaries(
    ci_report: RunReport, baseline_acc: float, current_acc: float, max_drop: float, ok: bool
) -> None:
    gate = check_baseline(
        _with_accuracy(ci_report, current_acc), _with_accuracy(ci_report, baseline_acc), max_drop
    )
    assert gate.ok is ok
    assert gate.drop_points == pytest.approx((baseline_acc - current_acc) * 100)


def test_check_baseline_refuses_different_subsets(ci_report: RunReport) -> None:
    with pytest.raises(BaselineError, match="subset"):
        check_baseline(ci_report, _with_accuracy(ci_report, 1.0, subset=None), 5)


def test_load_report_missing_baseline(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="not found"):
        load_report(tmp_path / "baseline.json")


# --- CLI ------------------------------------------------------------------------------------


def test_main_writes_results_and_gates(tmp_path: Path, ci_report: RunReport) -> None:
    out = tmp_path / "oracle.json"
    assert main(["--answerer", "evals.oracle:answer", "--subset", "ci", "--out", str(out)]) == 0
    written = RunReport.model_validate_json(out.read_text(encoding="utf-8"))
    assert written.summary.accuracy == 1.0
    assert written.meta.world == {"seed": 42, "end_date": "2026-10-27", "today": "2026-10-28"}
    assert len(written.meta.case_ids) == 15

    baseline = tmp_path / "baseline.json"
    baseline.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    dummy_args = ["--answerer", "evals.dummy:answer", "--subset", "ci", "--out", str(out)]
    gate = ["--baseline", str(baseline), "--fail-under-baseline", "5"]
    assert main([*dummy_args, *gate]) == 1
    oracle_args = ["--answerer", "evals.oracle:answer", "--subset", "ci", "--out", str(out)]
    assert main([*oracle_args, *gate]) == 0
    # baseline over a different subset -> configuration error
    assert main(["--answerer", "evals.oracle:answer", "--out", str(out), *gate]) == 2


def test_main_missing_answerer_or_baseline_exits_2(tmp_path: Path) -> None:
    out = tmp_path / "x.json"
    assert main(["--answerer", "evals.oracle:nope", "--out", str(out)]) == 2
    missing = ["--baseline", str(tmp_path / "missing.json"), "--fail-under-baseline", "5"]
    assert main(["--answerer", "evals.dummy:answer", "--out", str(out), *missing]) == 2
    assert not out.exists()


def test_results_json_is_plain_json(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    main(["--answerer", "evals.dummy:answer", "--subset", "ci", "--out", str(out)])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == {"meta", "summary", "cases"}
    assert data["summary"]["by_category"]["security"]["n"] == 2
