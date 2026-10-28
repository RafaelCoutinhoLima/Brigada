# Architecture

## Principles

1. **The model stays out of the domain.** Business rules and data access live in deterministic code exposed as MCP tools. The model decides *which* tool to call, never *how* data is fetched.
2. **Every LLM output that becomes data is validated.** Plans, tool arguments and final answers pass through Pydantic schemas.
3. **No silent failures.** Errors are logged, traced and surfaced. There is no fallback that pretends success.
4. **Everything measurable is measured.** Accuracy, latency and cost per question come from the eval suite, not intuition.

## Request lifecycle

1. `POST /ask` receives a question and a restaurant id.
2. **Router** classifies complexity and picks the model tier.
3. **Planner** produces a `Plan` (list of steps with specialist and dependencies).
4. **Executor** topologically sorts steps and runs independent ones concurrently.
5. Each **specialist** runs the agent runtime loop against the MCP server.
6. **Critic** checks the aggregated answer: every number must trace back to a tool result. It may request one extra round.
7. The answer is returned with the full trace id.

## Components

| Component | Path | Responsibility |
|-----------|------|----------------|
| Runtime | `src/brigada/runtime` | Tool-use loop, retries, timeouts, step limits, run persistence |
| Orchestration | `src/brigada/orchestration` | Planner, DAG executor, specialists, critic |
| MCP server | `src/brigada/mcp_server` | Read/write tools, human confirmation, audit log |
| RAG | `src/brigada/rag` | Indexing, hybrid retrieval, reranking |
| Router | `src/brigada/router` | Complexity classifier and model selection |
| Observability | `src/brigada/observability` | OpenTelemetry setup, cost accounting |
| API | `src/brigada/api` | FastAPI routes |
| Evals | `evals/` | Cases, metrics, judge, CI gate |

## Data model

```
restaurants(id, name, cuisine, city)
menu_items(id, restaurant_id, name, category, price, active)
orders(id, restaurant_id, created_at, total, channel)
order_items(order_id, menu_item_id, qty, unit_price)
deliveries(order_id, promised_minutes, actual_minutes)
reviews(id, restaurant_id, menu_item_id, rating, text, created_at, embedding vector)
runs(id, agent, restaurant_id, question, today, model, prompt_sha256, status,
     messages jsonb, steps, tokens_in, tokens_out, cost_usd, answer, error, started_at, finished_at)
steps(id, run_id, step, kind, payload jsonb, tokens_in, tokens_out, cost_usd, latency_ms)
tool_audit(id, run_id, tool, args jsonb, result jsonb, error, approved_by, created_at)
pending_actions(id, run_id, tool, restaurant_id, payload jsonb, status, decided_by, ...)
promotions(id, restaurant_id, menu_item_id, discount_pct, start_date, end_date, ...)
```

## Threat model (summary)

- Untrusted input: user question and retrieved review text.
- Highest-impact action: write tools that change the menu or create promotions.
- Defenses:
  - data/instruction separation: review text is returned as `untrusted_text`, and prompts state that tool results are data;
  - least privilege per agent: each specialist only sees its own tools (Sales has no write tools);
  - per-run restaurant scope, fail closed: the MCP server rejects any call without a bound scope or for another restaurant;
  - write tools only create pending actions; execution happens on a human path that is not an MCP tool;
  - once a run has read untrusted text, its write tools are disabled (also after a resume);
  - every call is audited, including rejected ones, with the trusted restaurant and `tool_use_id`; audit writes cannot fail on content;
  - database and internal errors never reach the model or the HTTP client; they are logged server-side;
  - automated attack tests (NUL bytes, scope types, SQL injection, cross-restaurant reads) run in CI.
