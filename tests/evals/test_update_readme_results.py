"""scripts/update_readme_results.py rewrites only the marked block."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.run import main as run_main
from scripts.update_readme_results import END, START, main, render_table, replace_between_markers

README = f"""# Title

## Results

{START}
| Metric | Value |
|--------|-------|
| Eval accuracy | – |
{END}

## Stack

untouched
"""


@pytest.fixture
def results_file(tmp_path: Path) -> Path:
    out = tmp_path / "results.json"
    assert run_main(["--answerer", "evals.oracle:answer", "--subset", "ci", "--out", str(out)]) == 0
    return out


def test_render_table(results_file: Path) -> None:
    table = render_table(json.loads(results_file.read_text(encoding="utf-8")))
    assert "| Eval accuracy | 100.0% (15/15, subset: ci) |" in table
    assert "| Router savings vs always-strong | – |" in table


def test_replace_only_between_markers() -> None:
    out = replace_between_markers(README, "NEW")
    assert out == README.split(START)[0] + f"{START}\nNEW\n{END}" + README.split(END)[1]
    assert out.endswith("## Stack\n\nuntouched\n")
    assert replace_between_markers(out, "NEW") == out  # idempotent


@pytest.mark.parametrize(
    "bad", ["no markers", f"{START} only", f"{END} {START}", f"{START}{START}{END}"]
)
def test_replace_rejects_bad_markers(bad: str) -> None:
    with pytest.raises(ValueError):
        replace_between_markers(bad, "x")


def test_main_rewrites_readme(tmp_path: Path, results_file: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(README, encoding="utf-8")
    assert main([str(results_file), "--readme", str(readme)]) == 0
    text = readme.read_text(encoding="utf-8")
    assert "100.0% (15/15, subset: ci)" in text
    assert text.startswith("# Title") and text.endswith("untouched\n")


def test_repo_readme_has_markers() -> None:
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    assert text.count(START) == 1 and text.count(END) == 1
