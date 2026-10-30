# Brigada

**A multi-agent platform for restaurant owners, built from scratch: no LangChain, no CrewAI, no agent framework.**

Ask a question like *"Why did my sales drop yesterday?"*, *"What do customers complain about in my menu?"* or *"What will run out before the next delivery?"*. A planner agent breaks it into steps, specialist agents query real data through an MCP server, a critic agent reviews the answer, and every step is traced with tokens, latency and cost.

> Status: 🚧 in active development. Runtime, MCP server, orchestration and the live console are in; RAG, router and the injection test suite are next.

## Why from scratch

Frameworks hide the parts that matter in production: the tool-use loop, retries, state, permissions, evaluation. Brigada implements each of them directly on top of the Claude API so every design decision is explicit and documented in [`docs/adr`](docs/adr).

## Architecture

```
               ┌──────────────┐
  question ──► │   Planner    │ ── JSON plan (validated) ──┐
               └──────────────┘                            ▼
                                       ┌──────────── DAG executor ─────────────┐
                                       │ Sales │ Menu │ Operations │ Inventory │  (parallel when independent)
                                       └───┬───┴──┬───┴─────┬──────┴────┬──────┘
                                            ▼        ▼         ▼
                                    ┌──────────── MCP server ────────────┐
                                    │ read tools │ write tools (human OK)│
                                    └─────┬──────────────┬───────────────┘
                                          ▼              ▼
                                     Postgres       pgvector (reviews, manuals)
                                            │
               ┌──────────────┐             ▼
  answer  ◄─── │    Critic    │ ◄── aggregated findings
               └──────────────┘
```

Cross-cutting: model router (ML classifier), guardrails, OpenTelemetry tracing, eval suite in CI.

Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Modules

| # | Module | Scope |
|---|--------|----------------|
| 1 | Agent runtime | Tool-use loop, parallel tool calls, retries, timeouts, step limits, resumable state |
| 2 | Orchestration | Planner-executor over a dependency graph, critic review |
| 3 | MCP server | Read/write tool separation, human-in-the-loop, append-only audit log |
| 4 | Hybrid RAG | Keyword + vector search, reranking, recall@k measured |
| 5 | Model router | Trained classifier choosing cheap vs strong model, accuracy vs cost |
| 6 | Evals & LLMOps | 62 cases with ground truth from the data generator, LLM-as-judge, versioned prompts, CI gate |
| 7 | Observability | Per-step traces, cost per question, prompt caching impact |
| 8 | Prompt injection defense | Malicious data in RAG, automated attack tests |

## Results

_Filled in as modules ship._

<!-- results:start -->
| Metric | Value |
|--------|-------|
| Eval accuracy | 94.0% (47/50, subset: all) |
| Tool precision / recall | 73.7% / 93.3% |
| Mean steps | 4.14 |
| p50 latency | 63.8 s |
| p95 latency | 167.9 s |
| Cost per question | US$ 0.0000 |
| Router savings vs always-strong | – |

_Answerer `brigada.orchestration:answer`, all 50 cases, commit `641eace`, 2026-10-29. Models: OpenRouter `:free` (nemotron-3-ultra for specialists and synthesis, nemotron-3.5-lightning for planner and critic). First real run: 60% on the ci subset._
<!-- results:end -->

Inventory (stock cover, dish margins, CMV, waste, cost changes, purchase proposals that need the owner's approval): 10/11 on the 11 inventory cases on the first live run with `nemotron-3.5-super-vl:free`; the miss (loss share computed over purchases instead of the counted stock) was fixed in the prompt and passes on re-run. Not yet included in the table above.

## Stack

Python 3.12 · FastAPI · Anthropic SDK (no agent framework) · OpenRouter adapter · MCP Python SDK · Postgres + pgvector (Supabase) · scikit-learn · OpenTelemetry · GitHub Actions · Docker · Railway

## Running locally

```bash
cp .env.example .env          # add a model API key (Anthropic, or OpenRouter) and DATABASE_URL
docker compose up -d db       # Postgres + pgvector; set BRIGADA_DB_PORT if 5432 is taken
uv sync
uv run python scripts/seed_data.py --reset
uv run pytest                 # add -m db to run tests against the database

# ask a specialist (needs a model API key); every step is saved in `runs`/`steps`
uv run python -m brigada.runtime "Quanto vendi ontem?" --restaurant 1 --today 2026-10-28
uv run python -m brigada.runtime "Qual prato tem mais reclamações?" --agent menu --restaurant 2
uv run python -m brigada.runtime "Onde as entregas atrasam?" --agent operations --restaurant 3
uv run python -m brigada.runtime --resume <run_id>   # continue a run that failed

# ask the orchestrator: planner -> specialists in parallel -> synthesis -> critic
uv run python -m brigada.orchestration "Por que minhas vendas caíram na última terça?" --restaurant 1 --today 2026-10-28

# live console: the orchestrator's plan and review plus one pane per agent, updated every second; dispatch questions from the prompt
uv run uvicorn brigada.api.main:app   # then open http://localhost:8000/console (needs API_KEYS)
# http://localhost:8000/console?demo  -> simulated runs, no server data or API cost

# write tools only propose; the owner decides in the console ("Aguardando sua decisão") or over HTTP
curl -H "X-API-Key: $KEY" "localhost:8000/actions?restaurant_id=1"
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"decided_by": "owner"}' localhost:8000/actions/<id>/approve   # or /reject
```

## Data

All restaurant data is **synthetic**, generated by `scripts/seed_data.py`. No real company or customer data is used.

## Author

Rafael Coutinho Lima · [LinkedIn](https://www.linkedin.com/in/rafaelcoutinholima) · [GitHub](https://github.com/RafaelCoutinhoLima)
