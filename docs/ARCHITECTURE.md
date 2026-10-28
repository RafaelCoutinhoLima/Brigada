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
runs(id, question, status, model, started_at, finished_at, cost_usd)
steps(id, run_id, agent, kind, payload jsonb, tokens_in, tokens_out, latency_ms)
tool_audit(id, run_id, tool, args jsonb, result jsonb, approved_by, created_at)
```

## Threat model (summary)

- Untrusted input: user question and retrieved review text.
- Highest-impact action: write tools that change the menu or create promotions.
- Defenses: data/instruction separation, write tools gated by human confirmation, write tools disabled when untrusted content is in context, automated injection tests.
