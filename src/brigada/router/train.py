"""Train the model router: a text classifier that predicts "the fast model is enough".

Data: ``evals/router_labels.jsonl`` (``scripts/build_router_labels.py``), one row per eval
question with whether the fast and the strong model answered it correctly in a live run.
The label is ``fast_passed``. Only the question text is a feature: the eval category is
metadata the router does not have at request time.

Evaluation is out-of-fold only: repeated stratified k-fold, every question predicted by a
model that never saw it. The routed system is then simulated with each question's real
result from the model it was sent to, and compared with always-strong, always-fast and the
oracle. The decision threshold is fixed (0.5), not tuned: with ~8 negatives a tuned threshold
would only fit the noise.

Usage::

    uv run python -m brigada.router.train
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import sklearn
from pydantic import BaseModel, ConfigDict
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import FeatureUnion, Pipeline

from brigada.observability.cost import get_pricing

DEFAULT_LABELS = Path("evals/router_labels.jsonl")
DEFAULT_MODEL = Path("models/router.joblib")
DEFAULT_METRICS = Path("evals/router_metrics.json")
THRESHOLD = 0.5


@dataclass(frozen=True)
class Price:
    """US$ per million tokens."""

    model: str
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.input_per_mtok + tokens_out * self.output_per_mtok) / 1e6

    @classmethod
    def of(cls, model: str) -> Price:
        """Standard-tier input/output price from the project's verified price table. No cache
        pricing: the eval runs report plain token totals, and caching would cut both models'
        cost by a similar factor."""
        standard = get_pricing(model).standard
        return cls(model, float(standard.input), float(standard.output))


# Hypothetical: the measured runs used free OpenRouter models. These are the Anthropic prices
# the same token counts would cost on the models the project targets.
STRONG_PRICE = Price.of("claude-sonnet-5-5")
FAST_PRICE = Price.of("claude-haiku-5-5")


class LabeledQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    question: str
    category: str
    fast_passed: bool | None
    strong_passed: bool | None
    fast_tokens_in: int | None = None
    fast_tokens_out: int | None = None
    strong_tokens_in: int | None = None
    strong_tokens_out: int | None = None

    @property
    def complete(self) -> bool:
        return self.fast_passed is not None and self.strong_passed is not None

    def cost(self, fast: bool) -> float:
        """What this question cost on the chosen model's run, at the hypothetical prices."""
        tin, tout = (
            (self.fast_tokens_in, self.fast_tokens_out)
            if fast
            else (self.strong_tokens_in, self.strong_tokens_out)
        )
        if tin is None or tout is None:
            raise ValueError(
                f"{self.id}: no token counts for the {'fast' if fast else 'strong'} run"
            )
        return (FAST_PRICE if fast else STRONG_PRICE).cost(tin, tout)


def load_labels(path: Path = DEFAULT_LABELS) -> list[LabeledQuestion]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [LabeledQuestion.model_validate_json(line) for line in lines if line.strip()]


def complete_rows(rows: Iterable[LabeledQuestion]) -> list[LabeledQuestion]:
    """Rows with both labels: the only ones the routed system can be simulated on."""
    return [r for r in rows if r.complete]


def target(rows: Sequence[LabeledQuestion]) -> np.ndarray:
    """1 = the fast model answered correctly, so sending it there would have been enough."""
    return np.array([int(bool(r.fast_passed)) for r in rows])


def build_pipeline(seed: int = 0) -> Pipeline:
    features = FeatureUnion(
        [
            # Character n-grams inside word boundaries: robust to Portuguese inflection and
            # typos ("vendi"/"vendas"), which matter more than word order on 60 questions.
            ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True)),
            ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2), sublinear_tf=True)),
        ]
    )
    # Balanced weights: the "fast fails" class is ~13% of the data; unweighted, the model
    # learns "always fast" and the router never sends anything to the strong model.
    clf = LogisticRegression(class_weight="balanced", C=1.0, max_iter=1000, random_state=seed)
    return Pipeline([("features", features), ("clf", clf)])


def fit(rows: Sequence[LabeledQuestion], seed: int = 0) -> Pipeline:
    y = target(rows)
    if len(set(y.tolist())) < 2:
        raise ValueError("training needs questions the fast model passed and failed")
    return build_pipeline(seed).fit([r.question for r in rows], y)


def fast_probability(pipeline: Pipeline, questions: Sequence[str]) -> np.ndarray:
    """P(fast model is enough) per question; column picked by class label, not position."""
    classes = list(pipeline.classes_)
    return pipeline.predict_proba(list(questions))[:, classes.index(1)]


def out_of_fold(rows: Sequence[LabeledQuestion], *, k: int, seed: int) -> np.ndarray:
    """P(fast is enough) for every question, each from a model trained without it."""
    y = target(rows)
    minority = int(min(y.sum(), len(y) - y.sum()))
    if not 2 <= k <= minority:
        raise ValueError(f"k={k} must be between 2 and the smaller class size ({minority})")
    questions = np.array([r.question for r in rows], dtype=object)
    proba = np.empty(len(rows))
    for train_idx, test_idx in StratifiedKFold(k, shuffle=True, random_state=seed).split(
        questions, y
    ):
        model = build_pipeline(seed).fit(list(questions[train_idx]), y[train_idx])
        proba[test_idx] = fast_probability(model, list(questions[test_idx]))
    return proba


@dataclass(frozen=True)
class Outcome:
    passed: int
    n: int
    to_fast: int
    cost_usd: float

    @property
    def accuracy(self) -> float:
        return self.passed / self.n

    @property
    def fast_fraction(self) -> float:
        return self.to_fast / self.n


def simulate(rows: Sequence[LabeledQuestion], to_fast: Sequence[bool]) -> Outcome:
    """The system that sends question i to the fast model iff ``to_fast[i]``: each question
    counts as passed when the model it went to passed it in the live run, and costs what that
    run spent on it."""
    if len(rows) != len(to_fast):
        raise ValueError("one routing decision per question")
    passed = 0
    cost = 0.0
    for row, fast in zip(rows, to_fast, strict=True):
        if not row.complete:
            raise ValueError(f"{row.id}: needs both labels to be simulated")
        passed += bool(row.fast_passed if fast else row.strong_passed)
        cost += row.cost(bool(fast))
    return Outcome(passed=passed, n=len(rows), to_fast=sum(map(bool, to_fast)), cost_usd=cost)


def oracle_routes(rows: Sequence[LabeledQuestion]) -> list[bool]:
    """Perfect hindsight: fast whenever fast passed, or when neither did (cheaper, same miss)."""
    return [bool(r.fast_passed) or not r.strong_passed for r in rows]


def wilson(passed: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion: honest at small n and near 0 or 1."""
    p = passed / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, center - half), min(1.0, center + half)


def _strategy(outcome: Outcome) -> dict[str, Any]:
    low, high = wilson(outcome.passed, outcome.n)
    return {
        **asdict(outcome),
        "accuracy": round(outcome.accuracy, 4),
        "accuracy_ci95": [round(low, 4), round(high, 4)],
        "fast_fraction": round(outcome.fast_fraction, 4),
        "cost_usd": round(outcome.cost_usd, 4),
    }


def _spread(values: Sequence[float]) -> dict[str, float]:
    return {
        "mean": round(float(np.mean(values)), 4),
        "min": round(float(np.min(values)), 4),
        "max": round(float(np.max(values)), 4),
    }


def evaluate(
    rows: Sequence[LabeledQuestion],
    *,
    k: int = 4,
    seeds: Sequence[int] = tuple(range(20)),
    threshold: float = THRESHOLD,
) -> dict[str, Any]:
    """All the numbers the README and the guide quote, out-of-fold only."""
    rows = list(rows)
    y = target(rows)
    n = len(rows)
    strong = simulate(rows, [False] * n)
    fast = simulate(rows, [True] * n)
    oracle = simulate(rows, oracle_routes(rows))

    routed: list[Outcome] = []
    aucs: list[float] = []
    caught: list[int] = []  # fast failures sent to the strong model
    wasted: list[int] = []  # fast successes sent to the strong model anyway
    for seed in seeds:
        proba = out_of_fold(rows, k=k, seed=seed)
        to_fast = (proba >= threshold).tolist()
        routed.append(simulate(rows, to_fast))
        aucs.append(float(roc_auc_score(y, proba)))
        caught.append(int(sum(1 for p, t in zip(to_fast, y, strict=True) if not p and t == 0)))
        wasted.append(int(sum(1 for p, t in zip(to_fast, y, strict=True) if not p and t == 1)))

    accs = [o.accuracy for o in routed]
    beats_fast = sum(o.passed > fast.passed for o in routed)
    return {
        "n": n,
        "fast_failures": int(n - y.sum()),
        "cv": {"k": k, "repeats": len(seeds), "threshold": threshold},
        "prices_per_mtok": {
            "strong": asdict(STRONG_PRICE),
            "fast": asdict(FAST_PRICE),
            "note": "hypothetical: the runs used free OpenRouter models",
        },
        "strategies": {
            "always_strong": _strategy(strong),
            "always_fast": _strategy(fast),
            "oracle": _strategy(oracle),
            "routed": {
                "accuracy": _spread(accs),
                "passed": _spread([o.passed for o in routed]),
                "fast_fraction": _spread([o.fast_fraction for o in routed]),
                "cost_usd": _spread([o.cost_usd for o in routed]),
            },
        },
        "classifier": {
            "roc_auc": _spread(aucs),
            "fast_failures_sent_to_strong": _spread(caught),
            "fast_successes_sent_to_strong": _spread(wasted),
        },
        "routed_beats_always_fast_in_repeats": beats_fast,
        "routed_savings_vs_always_strong": round(
            1 - float(np.mean([o.cost_usd for o in routed])) / strong.cost_usd, 4
        ),
    }


def verdict(metrics: dict[str, Any]) -> str:
    """The README line. The router has to beat its cheap alternative (always fast) in every
    CV repeat; otherwise the honest answer is that it is not worth it yet."""
    s = metrics["strategies"]
    routed, fast, strong = s["routed"], s["always_fast"], s["always_strong"]
    worth = metrics["routed_beats_always_fast_in_repeats"] == metrics["cv"]["repeats"]
    head = (
        f"{metrics['routed_savings_vs_always_strong'] * 100:.0f}% (estimate)"
        if worth
        else "not worth it yet"
    )
    return (
        f"{head}: routed {routed['accuracy']['mean'] * 100:.1f}% "
        f"(range {routed['accuracy']['min'] * 100:.1f}–{routed['accuracy']['max'] * 100:.1f}%) "
        f"vs always-fast {fast['accuracy'] * 100:.1f}% and always-strong "
        f"{strong['accuracy'] * 100:.1f}%, {routed['fast_fraction']['mean'] * 100:.0f}% sent "
        f"to fast; out-of-fold, n={metrics['n']}"
    )


def save_model(pipeline: Pipeline, path: Path, *, n: int, threshold: float = THRESHOLD) -> None:
    # joblib is pickle: loading it runs code. The file is produced here, kept out of git
    # (models/*.joblib) and loaded only from a path set by the operator, never from a request.
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "pipeline": pipeline,
            "threshold": threshold,
            "trained_on": n,
            "sklearn_version": sklearn.__version__,
        },
        path,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--metrics-out", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    rows = complete_rows(load_labels(args.labels))
    try:
        metrics = evaluate(rows, k=args.k, seeds=range(args.seed, args.seed + args.repeats))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    metrics["readme_row"] = verdict(metrics)
    save_model(fit(rows, args.seed), args.model_out, n=len(rows))
    args.metrics_out.parent.mkdir(parents=True, exist_ok=True)
    args.metrics_out.write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(metrics["strategies"], indent=2))
    print(metrics["readme_row"])
    print(f"model: {args.model_out}  metrics: {args.metrics_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
