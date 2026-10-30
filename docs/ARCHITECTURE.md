# Architecture

## Principles

1. **The model stays out of the domain.** Business rules and data access live in deterministic code exposed as MCP tools. The model decides *which* tool to call, never *how* data is fetched.
2. **Every LLM output that becomes data is validated.** Plans, tool arguments and final answers pass through Pydantic schemas.
3. **No silent failures.** Errors are logged, traced and surfaced. There is no fallback that pretends success.
4. **Everything measurable is measured.** Accuracy, latency and cost per question come from the eval suite, not intuition.

## Request lifecycle

1. `POST /ask` receives a question and a restaurant id.
2. **Router** classifies complexity and picks the model tier.
3. **Planner** produces a `Plan` (list of steps with specialist and dependencies), through a
   validated `submit_plan` tool, or a direct answer for out-of-scope questions.
4. **Executor** topologically sorts steps and runs independent ones concurrently. A step gets
   the findings of the steps it depends on; a step whose dependency failed is skipped.
5. Each **specialist** runs the agent runtime loop against the MCP server.
6. **Synthesizer** merges the findings into one answer (skipped when one step answered alone).
7. **Critic** checks the answer: every number and date must trace back to a tool result,
   helped by a deterministic list of the ones that do not (specialist prose is not a source;
   numbers that only appear in the owner's question are listed apart). It may request one
   extra round; if the answer is still rejected it ships with the critic's caveats. See ADR 0003.
8. The answer is returned with the full trace id and the orchestration id.

Model provider: Anthropic by default; OpenRouter through an adapter that speaks the same
Messages shape (`runtime/openrouter.py`, ADR 0004), selected by `MODEL_PROVIDER`.

## Components

| Component | Path | Responsibility |
|-----------|------|----------------|
| Runtime | `src/brigada/runtime` | Tool-use loop, retries, timeouts, step limits, run persistence; specialist registry (Sales, Menu, Operations, Inventory) |
| Orchestration | `src/brigada/orchestration` | Planner, DAG executor, synthesizer, critic, `answer()` |
| MCP server | `src/brigada/mcp_server` | Read/write tools, human confirmation, audit log |
| RAG | `src/brigada/rag` | Review search: Portuguese full-text, embeddings (OpenRouter), RRF fusion; index coverage reported to the caller; indexing in `rag/indexing.py` + `scripts/index_reviews.py` |
| Router | `src/brigada/router` | Complexity classifier and model selection |
| Observability | `src/brigada/observability` | OpenTelemetry setup, cost accounting |
| API | `src/brigada/api` | FastAPI routes; `/console` live view of each specialist's runs |
| Evals | `evals/` | Cases, metrics, judge, CI gate |

## Data model

```
restaurants(id, name, cuisine, city)
menu_items(id, restaurant_id, name, category, price, active)
orders(id, restaurant_id, created_at, total, channel)
order_items(order_id, menu_item_id, qty, unit_price)
deliveries(order_id, promised_minutes, actual_minutes)
reviews(id, restaurant_id, menu_item_id, rating, text, created_at, search tsvector generated)
review_embeddings(review_id, restaurant_id, model, embedding vector)  -- (review, restaurant) FK
purchase_orders(id, restaurant_id, ingredient_id, qty, status, source_action_id, ordered_at,
     received_at, received_by, unit_cost, movement_id, cancelled_at, cancelled_by)
runs(id, agent, restaurant_id, question, today, model, prompt_sha256, status,
     messages jsonb, steps, tokens_in, tokens_out, cost_usd, answer, error, started_at, finished_at,
     orchestration_id, plan_step, tainted)
orchestrations(id, restaurant_id, question, today, status, plan jsonb, review jsonb, rounds,
     tokens_in, tokens_out, cost_usd, answer, error, started_at, finished_at)
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
  - least privilege per agent: each specialist only sees its own tools (only Menu has write tools, and only Menu reads review text);
  - per-run restaurant scope, fail closed: the MCP server rejects any call without a bound scope or for another restaurant;
  - write tools only create pending actions; execution happens on a human path that is not an MCP tool;
  - once a run has read untrusted text, its write tools are disabled (also after a resume);
  - the same holds across agents: a step that receives findings derived from review text (or
    any step of the critic's extra round once a review was read) starts with writes disabled;
    the MCP server enforces it (the runtime sends the run's taint in `_meta`), so every
    blocked write is audited;
  - findings and answers are quoted to the synthesizer and critic with `<` and `>` neutralized,
    so customer text cannot close a tag and pose as orchestrator text;
  - the console caps in-flight work per restaurant and overall (429), since dispatch returns
    before the work is done;
  - every call is audited, including rejected ones, with the trusted restaurant and `tool_use_id`; audit writes cannot fail on content;
  - database and internal errors never reach the model or the HTTP client; they are logged server-side;
  - automated attack tests (NUL bytes, scope types, SQL injection, cross-restaurant reads) run in CI.
