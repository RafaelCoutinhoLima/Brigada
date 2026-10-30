"""Pure parts of review search: fusion, the offline embedder, the eval metrics."""

import math

import pytest

from brigada.rag.embeddings import HashingEmbedder
from brigada.rag.retrieval import RRF_K, rrf, to_pgvector
from evals.retrieval import is_relevant, metrics


def test_rrf_rewards_agreement_and_uses_only_ranks() -> None:
    fused = dict(rrf([[1, 2, 3], [3, 4]]))
    # 3 is in both lists: it beats 1, first in only one list.
    assert fused[3] == pytest.approx(1 / (RRF_K + 3) + 1 / (RRF_K + 1))
    assert fused[3] > fused[1] > fused[2]
    assert list(dict(rrf([[1, 2, 3], [3, 4]]))) == [3, 1, 2, 4]


def test_rrf_breaks_ties_by_id_so_results_are_stable() -> None:
    assert [d for d, _ in rrf([[7], [5]])] == [5, 7]


async def test_hashing_embedder_is_deterministic_normalized_and_accent_blind() -> None:
    e = HashingEmbedder(dim=64)
    a, b, c = await e.embed(["Porção FRIA", "porcao fria", "outra coisa"])
    assert a == b and a != c
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0)
    assert e.model == "hashing-64"


def test_pgvector_literal() -> None:
    assert to_pgvector([0.5, -1.0]) == "[0.5,-1]"


def test_metrics() -> None:
    m = metrics([False, True, True, False], total_relevant=2)
    assert m == {"r_precision": 0.5, "hit_at_5": 1.0, "mrr": 0.5}
    assert metrics([False] * 5, total_relevant=3)["mrr"] == 0.0


def test_relevance_is_accent_and_case_insensitive() -> None:
    assert is_relevant("Pouca porção pelo preço", ("pouca porcao",))
    assert not is_relevant("Muito bom", ("pouca porcao",))
