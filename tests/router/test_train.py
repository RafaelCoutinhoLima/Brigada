"""Router training: the classifier on separable synthetic data, and the routing simulation
checked by hand."""

import json
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.metrics import roc_auc_score

from brigada.router.train import (
    FAST_PRICE,
    STRONG_PRICE,
    LabeledQuestion,
    complete_rows,
    evaluate,
    fast_probability,
    fit,
    load_labels,
    main,
    oracle_routes,
    out_of_fold,
    simulate,
    target,
    verdict,
    wilson,
)

EASY = [
    "Quanto vendi ontem?",
    "Qual foi o faturamento de ontem?",
    "Quanto vendi na semana passada?",
    "Qual o faturamento do mês?",
    "Quanto vendi hoje?",
    "Qual foi o faturamento de sábado?",
    "Quanto vendi no domingo?",
    "Qual o faturamento da semana?",
]
HARD = [
    "Por que o lucro caiu e o que devo mudar no cardápio?",
    "Por que as avaliações pioraram e qual a causa?",
    "Por que o ticket médio caiu e o que fazer?",
    "Por que perdi clientes e o que devo mudar?",
    "Por que a margem caiu e qual prato devo tirar?",
    "Por que o delivery atrasou e o que mudar na cozinha?",
    "Por que o estoque acabou e o que devo comprar?",
    "Por que as vendas caíram e qual a causa?",
]


def _row(
    i: str, q: str, fast: bool, strong: bool, tokens: tuple[int, int, int, int] = (1, 1, 1, 1)
) -> LabeledQuestion:
    fi, fo, si, so = tokens
    return LabeledQuestion(
        id=i,
        question=q,
        category="x",
        fast_passed=fast,
        strong_passed=strong,
        fast_tokens_in=fi,
        fast_tokens_out=fo,
        strong_tokens_in=si,
        strong_tokens_out=so,
    )


def synthetic() -> list[LabeledQuestion]:
    """Easy questions: the fast model passes. Hard ones: only the strong model does."""
    easy = [_row(f"e{i}", q, True, True) for i, q in enumerate(EASY)]
    hard = [_row(f"h{i}", q, False, True) for i, q in enumerate(HARD)]
    return easy + hard


def test_target_is_fast_passed() -> None:
    rows = synthetic()
    assert target(rows).tolist() == [1] * len(EASY) + [0] * len(HARD)


def test_trained_router_sends_easy_to_fast_and_hard_to_strong() -> None:
    model = fit(synthetic())
    easy, hard = fast_probability(
        model, ["Quanto vendi na sexta?", "Por que o movimento caiu e o que devo mudar?"]
    )
    assert easy > 0.5 > hard


def test_out_of_fold_probabilities_rank_held_out_questions() -> None:
    rows = synthetic()
    proba = out_of_fold(rows, k=4, seed=0)
    assert roc_auc_score(target(rows), proba) > 0.9


def test_out_of_fold_is_reproducible() -> None:
    rows = synthetic()
    assert np.array_equal(out_of_fold(rows, k=4, seed=3), out_of_fold(rows, k=4, seed=3))


def test_k_larger_than_the_minority_class_is_refused() -> None:
    rows = synthetic()[: len(EASY) + 3]  # 3 negatives
    with pytest.raises(ValueError, match="smaller class"):
        out_of_fold(rows, k=4, seed=0)


def test_one_class_cannot_be_trained() -> None:
    with pytest.raises(ValueError, match="passed and failed"):
        fit(synthetic()[: len(EASY)])


def test_simulation_by_hand() -> None:
    # tokens: (fast_in, fast_out, strong_in, strong_out)
    rows = [
        _row("a", "a", True, True, (1_000_000, 0, 1_000_000, 0)),
        _row("b", "b", False, True, (0, 1_000_000, 0, 1_000_000)),
        _row("c", "c", True, False, (2_000_000, 0, 2_000_000, 0)),
        _row("d", "d", False, False, (0, 0, 0, 0)),
    ]
    out = simulate(rows, [True, False, True, False])
    # a: fast passes, $0.10. b: strong passes, $10. c: fast passes, $0.20. d: strong fails, $0.
    assert (out.passed, out.n, out.to_fast) == (3, 4, 2)
    assert out.accuracy == 0.75 and out.fast_fraction == 0.5
    assert out.cost_usd == pytest.approx(0.10 + 10.0 + 0.20)
    flipped = simulate(rows, [False, True, False, True])
    # a: strong passes $2. b: fast fails $0.50. c: strong fails $4. d: fast fails $0.
    assert (flipped.passed, flipped.to_fast) == (1, 2)
    assert flipped.cost_usd == pytest.approx(2.0 + 0.50 + 4.0)


def test_prices_are_per_million_tokens() -> None:
    assert STRONG_PRICE.cost(1_000_000, 1_000_000) == pytest.approx(12.0)
    assert FAST_PRICE.cost(1_000_000, 1_000_000) == pytest.approx(0.60)


def test_oracle_takes_fast_unless_only_strong_passes() -> None:
    rows = [
        _row("a", "a", True, True),
        _row("b", "b", False, True),
        _row("c", "c", True, False),
        _row("d", "d", False, False),
    ]
    assert oracle_routes(rows) == [True, False, True, True]


def test_a_row_without_both_labels_is_not_simulated() -> None:
    row = LabeledQuestion(id="x", question="q", category="c", fast_passed=True, strong_passed=None)
    assert complete_rows([row]) == []
    with pytest.raises(ValueError, match="both labels"):
        simulate([row], [True])


def test_missing_tokens_are_an_error_not_free() -> None:
    row = LabeledQuestion(id="x", question="q", category="c", fast_passed=True, strong_passed=True)
    with pytest.raises(ValueError, match="no token counts"):
        row.cost(True)


def test_wilson_interval() -> None:
    low, high = wilson(56, 62)
    assert low == pytest.approx(0.8045, abs=1e-3) and high == pytest.approx(0.9549, abs=1e-3)
    assert wilson(0, 10)[0] == 0.0 and wilson(10, 10)[1] == 1.0


def test_evaluate_on_separable_data_beats_always_fast() -> None:
    metrics = evaluate(synthetic(), k=4, seeds=[0, 1, 2])
    s = metrics["strategies"]
    assert s["always_fast"]["passed"] == len(EASY)
    assert s["always_strong"]["passed"] == s["oracle"]["passed"] == len(EASY) + len(HARD)
    assert s["routed"]["passed"]["min"] > s["always_fast"]["passed"]
    assert metrics["routed_beats_always_fast_in_repeats"] == 3
    assert verdict(metrics).startswith(f"{metrics['routed_savings_vs_always_strong'] * 100:.0f}%")


def test_verdict_when_routing_does_not_beat_always_fast() -> None:
    metrics = evaluate(synthetic(), k=4, seeds=[0])
    metrics["routed_beats_always_fast_in_repeats"] = 0
    assert verdict(metrics).startswith("not worth it yet")


def test_cli_writes_model_and_metrics(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_text("".join(r.model_dump_json() + "\n" for r in synthetic()), encoding="utf-8")
    model, metrics = tmp_path / "m" / "router.joblib", tmp_path / "metrics.json"
    argv = ["--labels", str(labels), "--model-out", str(model), "--metrics-out", str(metrics)]
    assert main([*argv, "--repeats", "2"]) == 0
    bundle = joblib.load(model)
    assert bundle["threshold"] == 0.5 and bundle["trained_on"] == len(EASY) + len(HARD)
    assert fast_probability(bundle["pipeline"], ["Quanto vendi ontem?"])[0] > 0.5
    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["cv"] == {"k": 4, "repeats": 2, "threshold": 0.5}
    assert data["readme_row"] == verdict(data)


def test_committed_labels_load() -> None:
    rows = load_labels()
    assert len(complete_rows(rows)) == 62
