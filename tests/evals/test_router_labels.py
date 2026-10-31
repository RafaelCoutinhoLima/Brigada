"""The router dataset: the script's output format, and the committed JSONL against the cases."""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.build_router_labels import FIELDS, build_rows, main, render

ROOT = Path(__file__).resolve().parents[2]
LABELS = ROOT / "evals" / "router_labels.jsonl"
CASES = ROOT / "evals" / "cases.jsonl"


def _case(case_id: str, passed: bool, tin: int = 100, tout: int = 10, **kw: Any) -> dict[str, Any]:
    return {
        "id": case_id,
        "question": kw.get("question", f"q {case_id}"),
        "category": kw.get("category", "sales"),
        "restaurant_id": 1,
        "passed": passed,
        "answer": {"tokens_in": tin, "tokens_out": tout, "steps": 2},
    }


def test_rows_join_both_runs_by_id() -> None:
    strong = {"cases": [_case("a", True, 200, 20), _case("b", True)]}
    fast = {"cases": [_case("b", False, 50, 5), _case("a", True), _case("c", True)]}
    rows = build_rows(strong, fast)
    assert [r["id"] for r in rows] == ["b", "a", "c"]
    b = rows[0]
    assert (b["fast_passed"], b["strong_passed"]) == (False, True)
    assert (b["fast_tokens_in"], b["fast_tokens_out"]) == (50, 5)
    a = rows[1]
    assert (a["strong_tokens_in"], a["strong_tokens_out"]) == (200, 20)
    c = rows[2]  # only in the fast run
    assert c["strong_passed"] is None and c["strong_tokens_in"] is None


def test_a_case_that_changed_between_runs_is_an_error() -> None:
    strong = {"cases": [_case("a", True, question="old")]}
    fast = {"cases": [_case("a", True, question="new")]}
    with pytest.raises(ValueError, match="differs"):
        build_rows(strong, fast)


def test_a_failed_case_without_answer_has_no_tokens() -> None:
    fast = {"cases": [{**_case("a", False), "answer": None}]}
    (row,) = build_rows({"cases": []}, fast)
    assert row["fast_passed"] is False and row["fast_tokens_in"] is None


def test_render_is_deterministic_jsonl() -> None:
    rows = build_rows({"cases": [_case("a", True)]}, {"cases": [_case("a", False)]})
    text = render(rows)
    assert text == render(rows) and text.endswith("\n") and text.count("\n") == 1
    assert list(json.loads(text)) == list(FIELDS)


def test_main_writes_what_render_returns(tmp_path: Path) -> None:
    strong = {"cases": [_case("a", True), _case("b", False)]}
    fast = {"cases": [_case("a", False), _case("b", True)]}
    (tmp_path / "s.json").write_text(json.dumps(strong))
    (tmp_path / "f.json").write_text(json.dumps(fast))
    out = tmp_path / "labels.jsonl"
    argv = ["--strong", str(tmp_path / "s.json"), "--fast", str(tmp_path / "f.json")]
    assert main([*argv, "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == render(build_rows(strong, fast))


def _committed() -> list[dict[str, Any]]:
    return [json.loads(line) for line in LABELS.read_text(encoding="utf-8").splitlines()]


def test_committed_labels_are_in_the_script_format() -> None:
    rows = _committed()
    # Re-rendering the parsed rows gives the same bytes: written by render(), not by hand.
    assert render(rows) == LABELS.read_text(encoding="utf-8")
    assert len({r["id"] for r in rows}) == len(rows)
    both = [r for r in rows if r["fast_passed"] is not None and r["strong_passed"] is not None]
    assert len(both) >= 60
    for r in both:
        assert isinstance(r["fast_passed"], bool) and isinstance(r["strong_passed"], bool)
        assert r["fast_tokens_in"] > 0 and r["strong_tokens_in"] > 0


def test_committed_labels_match_the_eval_cases() -> None:
    cases = {
        c["id"]: c
        for c in (json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines())
    }
    for r in _committed():
        assert r["id"] in cases, r["id"]
        assert r["question"] == cases[r["id"]]["question"], r["id"]
        assert r["category"] == cases[r["id"]]["category"], r["id"]
