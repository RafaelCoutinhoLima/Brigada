"""cases.jsonl is valid, complete and in sync with evals/build_cases.py."""

from __future__ import annotations

from collections import Counter

import pytest

from evals.build_cases import build, render, top_with_margin, validate
from evals.cases import CASES_PATH, load_cases
from evals.schema import CATEGORIES, WRITE_TOOLS, Case, NoToolCalledExpected

EXAMPLE_IDS = {"sales-001", "sales-002", "menu-001", "inject-001"}


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases()


def test_cases_file_in_sync_with_builder() -> None:
    assert CASES_PATH.read_text(encoding="utf-8") == render(build()), (
        "evals/cases.jsonl is stale: run `uv run python -m evals.build_cases`"
    )


def test_exactly_62_cases(cases: list[Case]) -> None:
    assert len(cases) == 62


def test_ids_and_questions_unique(cases: list[Case]) -> None:
    assert len({c.id for c in cases}) == len(cases)
    assert len({(c.restaurant_id, c.question.strip().lower()) for c in cases}) == len(cases)


def test_example_ids_kept(cases: list[Case]) -> None:
    assert EXAMPLE_IDS <= {c.id for c in cases}


def test_all_restaurants_and_categories_covered(cases: list[Case]) -> None:
    assert {c.restaurant_id for c in cases} == {1, 2, 3}
    assert {c.category for c in cases} == set(CATEGORIES)


def test_ci_subset_has_16_covering_all_categories() -> None:
    ci = load_cases(subset="ci")
    assert len(ci) == 16
    assert {c.category for c in ci} == set(CATEGORIES)


def test_security_and_refusal_minimums(cases: list[Case]) -> None:
    counts = Counter(c.category for c in cases)
    assert counts["security"] >= 6
    assert counts["out_of_scope"] >= 3
    guarded = [
        c
        for c in cases
        if c.category == "security"
        and (
            (isinstance(c.expected, NoToolCalledExpected) and c.expected.tools)
            or set(WRITE_TOOLS) & set(c.forbidden_tools)
        )
    ]
    assert len(guarded) >= 6


def test_planted_outage_case_matches_planted_facts(cases: list[Case]) -> None:
    by_id = {c.id: c for c in cases}
    assert by_id["menu-001"].expected.model_dump()["value"] == "Feijoada da casa"
    assert by_id["sales-001"].expected.model_dump()["value"] > 0


def test_validate_rejects_duplicate_questions(cases: list[Case]) -> None:
    dupe = cases[0].model_copy(update={"id": "sales-999"})
    with pytest.raises(ValueError, match="unique"):
        validate([cases[0], dupe])


def test_validate_rejects_echoed_contains_any() -> None:
    case = Case.model_validate(
        {
            "id": "diag-999",
            "restaurant_id": 3,
            "question": "Por que atrasa no sábado?",
            "category": "diagnosis",
            "expected": {"type": "contains_any", "values": ["sábado"]},
        }
    )
    with pytest.raises(ValueError, match="echo"):
        validate([case])


def test_top_with_margin_rejects_ties() -> None:
    assert top_with_margin([("a", 12), ("b", 10)], "x") == "a"
    with pytest.raises(ValueError, match="margin"):
        top_with_margin([("a", 10.5), ("b", 10)], "x")


def test_case_schema_rejects_conflicting_tools() -> None:
    with pytest.raises(ValueError):
        Case.model_validate(
            {
                "id": "inject-999",
                "restaurant_id": 1,
                "question": "x?x",
                "category": "security",
                "expected": {"type": "refusal"},
                "expected_tools": ["pause_menu_item"],
                "forbidden_tools": ["pause_menu_item"],
            }
        )


def test_case_schema_rejects_unknown_tool_and_extra_fields() -> None:
    base = {
        "id": "sales-999",
        "restaurant_id": 1,
        "question": "Quanto?",
        "category": "sales",
        "expected": {"type": "number", "value": 1},
    }
    with pytest.raises(ValueError):
        Case.model_validate({**base, "expected_tools": ["drop_database"]})
    with pytest.raises(ValueError):
        Case.model_validate({**base, "surprise": True})
