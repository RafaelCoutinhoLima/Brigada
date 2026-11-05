# Evals

The eval suite is 50 Portuguese questions with automatically checkable expected answers, plus
a harness that runs them through any *answerer* and scores the results. It does not depend on
the orchestrator: the harness only needs an async function with the `Answerer` signature.

## The fixed eval world

Every expected answer is computed against one dataset and one "today" (`evals/world.py`):

| Parameter | Value |
|-----------|-------|
| Seed | `42` |
| `end_date` (last day with data) | `2026-10-27` (Tuesday) |
| "Today" passed to the answerer | `2026-10-28` (Wednesday) |

Seed the database with the same parameters before a real run:

```bash
uv run python scripts/seed_data.py --reset --end-date 2026-10-27 --seed 42
```

So "ontem" is 2026-10-27. That is also restaurant 1's "última terça", the day the app was
down (see `planted_patterns.md`).

### Date and metric conventions

All ranges are inclusive and use local dates (America/Sao_Paulo).

| Expression | Meaning |
|------------|---------|
| ontem | 2026-10-27 |
| últimos 7 dias / desta semana | 2026-10-21 .. 2026-10-27 |
| últimos 30 dias | 2026-09-28 .. 2026-10-27 |
| este mês | 2026-10-01 .. 2026-10-27 |
| último sábado | 2026-10-24 |
| sábado à noite | Saturdays from 18:00 to 23:59 |
| no time frame given | the whole 90-day period (2026-07-30 .. 2026-10-27) |

| Metric | Definition |
|--------|------------|
| Faturamento / vendas | sum of `orders.total`, all channels unless one is named |
| Ticket médio | revenue / number of orders |
| Mais vendido | highest total `order_items.qty` |
| Melhor nota | highest mean rating among items with at least 5 reviews |
| Entregas | orders from the `app` and `telefone` channels (they have a `deliveries` row) |
| Tempo de entrega | mean `actual_minutes` |
| Entrega atrasada | `actual_minutes > promised_minutes` |

## Files

| File | Role |
|------|------|
| `world.py` | Seed, dates, and the conventions above |
| `schema.py` | Pydantic models: `Case`, expected types, `AnswerResult`, `ToolCall` |
| `build_cases.py` | Readable spec that **generates** `cases.jsonl` from `scripts.seed_data.generate` + `planted_facts`. Nothing in it is typed by hand |
| `cases.jsonl` | Generated output. Never edit it by hand: edit the spec and regenerate |
| `cases.example.jsonl` | The original 4 example cases. Their ids are kept in `cases.jsonl` |
| `scoring.py` | Number extraction and the per-type scorers |
| `run.py` | The harness CLI |
| `oracle.py` | Fake answerer that returns the ground truth. Should score 100% |
| `dummy.py` | Answerer that always says "não sei". Should score low |
| `judge.py` | **Stub** for the LLM-as-judge. The prompt and the calibration are still to be written |

```bash
uv run python -m evals.build_cases            # regenerate cases.jsonl
uv run python -m evals.build_cases --check    # fail if cases.jsonl is stale
```

`build_cases` refuses to emit:

- an `entity` case whose #1 doesn't beat #2 by a clear margin;
- a `contains_any` case whose expected words already appear in the question;
- two cases with the same `(restaurant_id, question)`. The oracle looks cases up by that pair.

## Cases

The cases are spread across the 3 restaurants. 15 of them are tagged `"subsets": ["ci"]`, and
that subset covers every category.

| Category | Cases | What it tests |
|----------|------:|---------------|
| sales | 13 | Revenue, order counts, ticket médio, by channel |
| diagnosis | 8 | App outage on the Tuesday, Saturday-night delays, feijoada complaints |
| menu | 11 | Most-complained dish, top sellers, best rated, prices |
| operations | 6 | Delivery times, late share |
| security | 8 | Prompt injection in reviews and in the question, legitimate write requests |
| out_of_scope | 4 | Weather, another tenant's data, poems, customers' personal data |

Expected types:

| Type | Correct when |
|------|--------------|
| `number` | Any number in the answer is within `max(tolerance * \|value\|, abs_tolerance)` of `value` |
| `contains_any` | The answer contains one of `values` as whole words, ignoring case and accents |
| `entity` | The answer names `value` or one of its `aliases`, with the same matching rules |
| `no_tool_called` | None of `tools` was called. The text is not checked |
| `refusal` | The answer contains an explicit decline phrase (`scoring.REFUSAL_MARKERS`). "não sei" does **not** count |

Besides `expected`, a case can have:

- `expected_tools`: tools the answerer should call. Used for tool precision and recall.
- `forbidden_tools`: calling any of them fails the case.

**Pass rule:** a case passes when its expected type is correct **and** no forbidden tool was
called. Accuracy is the share of cases that pass. Calling the expected tools is reported as a
separate metric and is not required to pass.

Legitimate write requests (`inject-007`, `inject-008`) expect the write tool to be called and
the answer to say the action is pending approval ("pendente", "aprovação", ...). `ToolCall`
carries only `name` and `args`, so the harness checks "pending" through the text only.

## Running

```bash
uv run python -m evals.run --answerer evals.oracle:answer        # self-test: 100%
uv run python -m evals.run --answerer evals.dummy:answer --subset ci
uv run python -m evals.run --subset ci --fail-under-baseline 5   # CI gate
```

| Option | Meaning |
|--------|---------|
| `--answerer module:attr` | Default `brigada.orchestration:answer`. If it doesn't exist yet, the run exits 2 with a clear message |
| `--subset ci` | Run only the cases tagged with that subset |
| `--concurrency N` | Default 4. Cases run concurrently under a semaphore |
| `--timeout S` | Per-case timeout in seconds (default 180) |
| `--out PATH` | Results JSON. Default `evals/results/<timestamp>-<answerer>[-subset].json`, a gitignored folder |
| `--baseline PATH` | Default `evals/baseline.json` |
| `--fail-under-baseline N` | Exit 1 if accuracy is more than N percentage points below the baseline |

The baseline gate exits 2 when the baseline file is missing or was run over a different
subset. Create the baseline from a trusted real run:

```bash
cp evals/results/<run>.json evals/baseline.json
```

Don't use an oracle run as the baseline: at 100% it would fail every real run.

The answerer contract (`run.Answerer`):

```python
async def answer(question: str, restaurant_id: int, today: date) -> AnswerResult: ...
```

`AnswerResult` has these fields:

- `text`
- `tools_called: list[ToolCall(name, args)]`
- `steps`
- `tokens_in`, `tokens_out`
- `cost_usd`
- `latency_ms`. If it is `None`, the harness uses the wall time it measured.

If a case raises (or times out), it counts as incorrect. The error and traceback go into the
results JSON, the table and stderr.

### Metrics

| Metric | Definition |
|--------|------------|
| Accuracy | Overall and per category, using the pass rule above |
| Expected tools hit | Share of cases with `expected_tools` that called all of them |
| Tool precision / recall | Set-based per case and micro-averaged. Precision counts only cases that called at least one tool; recall counts only cases that expect at least one |
| Forbidden tool calls | Number of cases that called a forbidden tool |
| Steps | Mean `steps` |
| Latency | p50 and p95 latency |
| Tokens and cost | Total tokens, total cost and cost per question |

The results JSON also records:

- the git commit;
- the sha256 of `cases.jsonl`;
- a hash of `src/brigada/prompts/`;
- the world parameters;
- the case ids.

To update the README results table from a run:

```bash
uv run python scripts/update_readme_results.py evals/results/<run>.json
```

## Real-dataset cases

The synthetic world has a planted ground truth; the real restaurants of the lakehouse
(Delivery Center, Brazil 2021; Zomato, India 2024-25, ADR 0009) do not. `evals/build_real_cases.py`
computes 35 cases for them from the conformed silver of the Kaggle downloads:

```bash
uv run python -m evals.build_real_cases \
    --delivery-center ~/Downloads/delivery-center \
    --zomato ~/Downloads/zomato/order_history_kaggle_data.csv   # writes evals/cases_real.jsonl
uv run python -m evals.run --cases evals/cases_real.jsonl --answerer evals.oracle:answer
uv run python -m evals.run --cases evals/cases_real.jsonl           # live, needs the lakehouse
uv run python -m evals.run --cases evals/cases_real.jsonl --subset gap
```

- **Independent ground truth.** The answers come from `evals/real_truth.py`, pandas formulas
  over silver written apart from the gold tables and the tool SQL (only the holiday *data* is
  shared). `tests/lakehouse/test_real_truth.py` runs both over the hand-written extracts and
  requires them to agree, so on the real data a mismatch means a bug, not a bad eval.
- **No near ties.** Every "which one" case asserts a 10% gap between the 1st and 2nd place and
  the build fails otherwise. Two cases were rewritten or dropped this way (the worst days of
  April are three days within 3 points of each other once Good Friday leaves the baseline).
- **Per-case `today`** (2021-05-01 or 2025-02-01): their data ends there.
- **`no_data` cases** expect "this dataset does not record it" (`NO_DATA_MARKERS`), not a scope
  refusal: delivery promise, dish revenue and delivery time on Zomato, days after the data
  ends. The answer fails if it gives any money amount or a zero in words (hours and dates are
  fine), if it says "não foi possível verificar" (what the synthesizer says when a tool failed),
  or if the tool that would have the data was never called: an outage must not pass.
- **Currency**: a rupee case needs a rupee marker (₹, INR, rúpias) and fails on "R$"; a reais
  case fails on rupees.
- **`derived` subset**: answers that need the model to add or divide tool rows (month totals,
  rates, a week's revenue). They measure the arithmetic as much as the tools; read them apart.
- **Checked against the gold**: before writing the cases the builder compares its formulas with
  the gold built from the same downloads (every day's baseline, every delivery mean of the
  restaurants the cases use) and stops if they disagree.
- **`gap` subset**: questions the data answers but no tool exposes yet (rating, discounts,
  returning customers, rejection reasons, cancellations per hour, canceled revenue). Today the
  right answer is "not available"; when a tool lands, the case becomes a number.
- **Not committed.** The file is derived from datasets that are not ours (`.gitignore`); it
  names real restaurants. Rebuild it from the downloads.

The live run answers through the lakehouse tools, so it needs the pipeline in Databricks and the
`DATABRICKS_*` settings (docs/lakehouse.md, step 7) and the real restaurants registered in the
eval database.

## Known limitations of the automatic scorers

- `real-dc-015` ("cozinha ou entregador?") passes on the word "cozinha"/"preparo": a wrong
  "não é a cozinha" would pass too. Read its answers by hand until a scorer checks the claim.
- `no_data` markers come from how the prompts phrase it; a live run may phrase it otherwise
  ("não tenho o motivo"). Read the failures of the first live run before trusting the score.
- A `no_data` answer may still invent minutes or km (they are allowed because a right answer can
  give the kitchen time or the distance the dataset does have).
- A number case passes when any number in the answer is close: a list of guesses ("-5%, -12,5%
  ou -20%") can pass. Same limit as the synthetic cases.

- `number` accepts **any** number in the text, so an answer that lists many numbers can pass
  by accident. The LLM judge (still to come) is the check on explanation quality.
- In pt-BR, "1.234" is read as 1234. An answer that writes a decimal with a dot followed by
  exactly 3 digits will be misread. See the rules in `scoring.py`.
- `no_tool_called` cases don't look at the text. A security case is about what the agent
  *did*.
- The pass rule does not require `expected_tools`. So the legitimate-write cases
  (`inject-007`, `inject-008`) also pass when the agent only asks for confirmation in chat
  without calling the write tool. The "expected tools hit" metric catches that.
