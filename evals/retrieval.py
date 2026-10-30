"""Retrieval eval for review search: keyword vs vector vs hybrid, on labeled queries.

The reviews come from a small set of templates (see ``scripts.seed_data``), so a relevant set
is "every review whose text has this phrase", not one document. That is why the metrics are:

- R-precision: precision in the top R, where R = number of relevant reviews (capped at 10, so
  a phrase present in 28 reviews is not a test of recall@28);
- hit@5: at least one relevant review in the top 5;
- MRR: 1 / rank of the first relevant review.

The queries are written to separate the strategies: paraphrases that share no word with the
reviews (vector should win), rare exact tokens (keyword should win), and the planted injection
reviews (an attacker's text must be findable, which is why the search tool taints the run).

    uv run python -m evals.retrieval                 # real embedding model (OpenRouter)
    uv run python -m evals.retrieval --hashing       # offline embedder (CI smoke run)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brigada.mcp_server.db import Conn, connect
from brigada.rag.embeddings import Embedder, HashingEmbedder, default_embedder
from brigada.rag.retrieval import Mode, missing_reviews, search, store_embeddings
from scripts.seed_data import INJECTION_TEXTS

R_CAP = 10
MODES: tuple[Mode, ...] = ("keyword", "vector", "hybrid")
INJECTION = "__injection__"


@dataclass(frozen=True)
class Query:
    restaurant_id: int
    text: str
    relevant_phrases: tuple[str, ...]  # a review is relevant if its text has any of them
    kind: str  # "paraphrase" | "exact" | "injection"


QUERIES: tuple[Query, ...] = (
    # Restaurant 2: the feijoada complaints (cold, small portion).
    Query(2, "comida chegou fria", ("frio",), "exact"),
    Query(2, "comida gelada", ("frio",), "paraphrase"),
    Query(2, "a refeição veio sem temperatura", ("frio",), "paraphrase"),
    Query(2, "porção pequena", ("pouca porção",), "exact"),
    Query(2, "quantidade pequena de comida", ("pouca porção",), "paraphrase"),
    Query(2, "serviram muito pouco", ("pouca porção",), "paraphrase"),
    Query(2, "saí com fome", ("saí com fome",), "exact"),
    Query(2, "precisei esquentar em casa", ("micro-ondas",), "paraphrase"),
    Query(2, "micro-ondas", ("micro-ondas",), "exact"),
    Query(2, "comida sem sabor", ("sem graça", "perdeu todo o sabor"), "paraphrase"),
    Query(2, "prato sem graça", ("sem graça",), "exact"),
    Query(2, "caro para o que entrega", ("pelo preço",), "paraphrase"),
    # Restaurant 1.
    Query(1, "embalagem fraca", ("embalagem",), "exact"),
    Query(1, "o pacote chegou ruim", ("embalagem",), "paraphrase"),
    Query(1, "demorou para chegar", ("demorou",), "exact"),
    Query(1, "atraso no pedido", ("demorou",), "paraphrase"),
    Query(1, "recomendo", ("recomendo",), "exact"),
    Query(1, "indicaria para os amigos", ("recomendo",), "paraphrase"),
    Query(1, "vale o que custa", ("custo-benefício",), "paraphrase"),
    Query(1, "a qualidade caiu", ("já foi melhor",), "paraphrase"),
    # Restaurant 3, including the planted injections.
    Query(3, "embalagem poderia ser melhor", ("embalagem",), "exact"),
    Query(3, "entrega lenta", ("demorou",), "paraphrase"),
    Query(3, "bom custo-benefício", ("custo-benefício",), "exact"),
    Query(3, "piorou com o tempo", ("já foi melhor",), "paraphrase"),
    Query(3, "nunca decepciona", ("nunca decepciona",), "exact"),
    Query(3, "sempre peço e gosto", ("nunca decepciona", "peço sempre"), "paraphrase"),
    Query(3, "avaliação dando ordens para o assistente", (INJECTION,), "injection"),
    Query(3, "pedido de compra de salmão", (INJECTION,), "injection"),
    Query(3, "promoção de desconto em todos os itens", (INJECTION,), "injection"),
    Query(3, "ignorar instruções anteriores", (INJECTION,), "injection"),
)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def is_relevant(text: str, phrases: Sequence[str]) -> bool:
    if INJECTION in phrases:
        return text in INJECTION_TEXTS
    t = _norm(text)
    return any(_norm(p) in t for p in phrases)


def metrics(ranked_relevance: Sequence[bool], total_relevant: int) -> dict[str, float]:
    """R-precision, hit@5 and MRR of one ranked list (True = relevant)."""
    r = min(total_relevant, R_CAP)
    first = next((i for i, rel in enumerate(ranked_relevance, start=1) if rel), None)
    return {
        "r_precision": sum(ranked_relevance[:r]) / r if r else 0.0,
        "hit_at_5": 1.0 if any(ranked_relevance[:5]) else 0.0,
        "mrr": 1.0 / first if first else 0.0,
    }


async def evaluate(conn: Conn, embedder: Embedder) -> dict[str, Any]:
    texts = {
        r["id"]: (r["restaurant_id"], r["text"])
        for r in conn.execute("SELECT id, restaurant_id, text FROM reviews").fetchall()
    }
    missing = missing_reviews(conn, embedder.model)
    if missing:  # the index must cover the corpus, or the vector leg is measured on a subset
        vectors = await embedder.embed([r["text"] for r in missing])
        with conn.transaction():
            store_embeddings(conn, embedder.model, missing, vectors)
    query_vectors = await embedder.embed([q.text for q in QUERIES])
    rows = []
    for q, qv in zip(QUERIES, query_vectors, strict=True):
        total = sum(
            1
            for rid, t in texts.values()
            if rid == q.restaurant_id and is_relevant(t, q.relevant_phrases)
        )
        row: dict[str, Any] = {
            "query": q.text,
            "restaurant_id": q.restaurant_id,
            "kind": q.kind,
            "relevant": total,
        }
        for mode in MODES:
            hits = search(
                conn,
                q.restaurant_id,
                q.text,
                k=R_CAP,
                mode=mode,
                model=embedder.model,
                query_vector=qv,
            )
            assert all(texts[h.review_id][0] == q.restaurant_id for h in hits), "tenant leak"
            rel = [is_relevant(texts[h.review_id][1], q.relevant_phrases) for h in hits]
            row[mode] = metrics(rel, total)
        rows.append(row)
    return {
        "model": embedder.model,
        "at": datetime.now(UTC).isoformat(),
        "queries": rows,
        "summary": summarize(rows),
    }


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, dict[str, float]]]:
    """Mean of each metric per mode, overall and per query kind."""
    groups: dict[str, list[dict[str, Any]]] = {"all": list(rows)}
    for r in rows:
        groups.setdefault(r["kind"], []).append(r)
    return {
        g: {
            mode: {
                m: round(sum(r[mode][m] for r in rs) / len(rs), 3)
                for m in ("r_precision", "hit_at_5", "mrr")
            }
            for mode in MODES
        }
        for g, rs in groups.items()
    }


def table(summary: dict[str, dict[str, dict[str, float]]]) -> str:
    lines = [
        "| Strategy | R-precision | hit@5 | MRR | R-precision on paraphrases |",
        "|---|---|---|---|---|",
    ]
    for mode in MODES:
        a, p = summary["all"][mode], summary.get("paraphrase", {}).get(mode, {})
        lines.append(
            f"| {mode} | {a['r_precision']:.2f} | {a['hit_at_5']:.2f} | "
            f"{a['mrr']:.2f} | {p.get('r_precision', 0):.2f} |"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review retrieval eval.")
    parser.add_argument("--hashing", action="store_true", help="offline embedder")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    embedder: Embedder = HashingEmbedder() if args.hashing else default_embedder()
    with connect() as conn:
        result = asyncio.run(evaluate(conn, embedder))
    out = (
        args.out
        or Path(__file__).parent
        / "results"
        / f"retrieval-{embedder.model.replace('/', '_').replace(':', '_')}.json"
    )
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(table(result["summary"]))
    print(f"\nresults: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
