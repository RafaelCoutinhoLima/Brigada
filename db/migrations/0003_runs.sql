-- Agent runs and their steps: what was asked, what it cost, and enough state to resume.

CREATE TABLE IF NOT EXISTS runs (
    id text PRIMARY KEY,
    agent text NOT NULL,
    restaurant_id integer NOT NULL REFERENCES restaurants,
    question text NOT NULL,
    -- Reference date for "ontem", "última terça"... A resume must reuse it, not today's date.
    today date NOT NULL,
    model text NOT NULL,
    prompt_sha256 text NOT NULL,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed')),
    -- The full conversation sent to the model. The API is stateless, so this is the state.
    messages jsonb NOT NULL,
    steps integer NOT NULL DEFAULT 0,
    tokens_in integer NOT NULL DEFAULT 0,
    tokens_out integer NOT NULL DEFAULT 0,
    cost_usd numeric(12, 6) NOT NULL DEFAULT 0,
    answer text,
    error text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS steps (
    id bigserial PRIMARY KEY,
    run_id text NOT NULL REFERENCES runs ON DELETE CASCADE,
    step integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('llm_call', 'tool_results')),
    payload jsonb NOT NULL,
    tokens_in integer NOT NULL DEFAULT 0,
    tokens_out integer NOT NULL DEFAULT 0,
    cost_usd numeric(12, 6) NOT NULL DEFAULT 0,
    latency_ms integer,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS steps_run_id_idx ON steps (run_id, step);
CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status, started_at);
