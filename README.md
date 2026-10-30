# Brigada

**A multi-agent platform for restaurant owners, built from scratch: no LangChain, no CrewAI, no agent framework.**

Ask a question like *"Why did my sales drop yesterday?"*, *"What do customers complain about in my menu?"* or *"What will run out before the next delivery?"*. A planner agent breaks it into steps, specialist agents query real data through an MCP server, a critic agent reviews the answer, and every step is traced with tokens, latency and cost.

> Status: 🚧 in active development. Runtime, MCP server, orchestration, inventory, review search, the owner's approval flow and the live console are in; the model router and a calibrated LLM judge are next.

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
| 4 | Review search (RAG) | Portuguese full-text + embeddings + RRF over customer reviews, measured on 30 labeled queries |
| 5 | Model router (planned) | Trained classifier choosing cheap vs strong model, accuracy vs cost |
| 6 | Evals & LLMOps | 63 cases with ground truth from the data generator, versioned prompts, CI gate; LLM-as-judge planned |
| 7 | Observability | Per-step traces, cost per question, prompt caching impact |
| 8 | Prompt injection defense | Malicious data in RAG, automated attack tests |

## Results

_Filled in as modules ship._

<!-- results:start -->
| Metric | Value |
|--------|-------|
| Eval accuracy | 90.3% (56/62, subset: all) |
| Tool precision / recall | 74.6% / 93.0% |
| Mean steps | 4.34 |
| p50 latency | 37.7 s |
| p95 latency | 93.3 s |
| Cost per question | US$ 0.0000 |
| Router savings vs always-strong | – |

_Answerer `brigada.orchestration:answer`, all 62 cases including the 11 inventory ones, commit `3d12ae8`, 2026-10-30. Models: OpenRouter `:free` (nemotron-3.5-super-vl for specialists and synthesis, nemotron-3.5-lightning for planner and critic). Earlier runs: 94% on the 50 cases without inventory; 60% on the first real run. One of the 6 misses (inv-010: the model sent `category="None"` and read the empty list as "no margins") is fixed since; `inject-010` (an injection found by search) was added after this run._
<!-- results:end -->

### Review search

`search_reviews` (menu specialist) finds what customers say about a topic. On 30 labeled queries over the seeded reviews (`uv run python -m evals.retrieval`, embeddings `nvidia/nemotron-3.5-embed-1b:free`; relevant set = every review with the phrase, R capped at 10):

| Strategy | R-precision | hit@5 | MRR | R-precision on paraphrases |
|---|---|---|---|---|
| keyword (Postgres full-text, `portuguese` + unaccent) | 0.55 | 0.67 | 0.67 | 0.31 |
| vector (cosine, pgvector) | **0.81** | **0.90** | **0.91** | **0.78** |
| hybrid (RRF, k=60) | 0.74 | 0.90 | 0.89 | 0.67 |

R-precision by query kind (12 exact wordings, 14 paraphrases, 4 planted injections): keyword 0.91 / 0.31 / 0.44, vector 1.00 / 0.78 / 0.38, hybrid 0.95 / 0.67 / 0.44.

Vector search wins on this corpus, so it is the tool's default. Every downgrade is reported in the result: `hybrid` when part of the reviews has no vector yet (the keyword leg finds them), `keyword` when embedding fails or the index is empty. Hybrid lost because OR-ed keyword matches add noise (the Portuguese stemmer maps *comida* and *comi* to the same stem) that RRF promotes. It only helped on the planted injection reviews. Reviews are customer text, so a search taints the run and disables write tools, like `reviews_for_item`.


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
# an approved purchase becomes an order ("A caminho" in the console); receiving it adds the stock
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"received_by": "owner", "unit_cost": "41.90"}' localhost:8000/purchase-orders/<id>/receive
```

## Data

All restaurant data is **synthetic**, generated by `scripts/seed_data.py`. No real company or customer data is used.

## Author

Rafael Coutinho Lima · [LinkedIn](https://www.linkedin.com/in/rafaelcoutinholima) · [GitHub](https://github.com/RafaelCoutinhoLima)
