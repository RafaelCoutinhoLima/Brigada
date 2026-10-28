-- One row per question answered by the orchestrator: the validated plan, the critic's review
-- and the totals. Every model call it made (planner, specialists, synthesizer, critic) is a
-- row in `runs` pointing back here.

CREATE TABLE IF NOT EXISTS orchestrations (
    id text PRIMARY KEY,
    restaurant_id integer NOT NULL REFERENCES restaurants,
    question text NOT NULL,
    today date NOT NULL,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed')),
    plan jsonb,
    review jsonb,
    rounds integer NOT NULL DEFAULT 0,
    tokens_in integer NOT NULL DEFAULT 0,
    tokens_out integer NOT NULL DEFAULT 0,
    cost_usd numeric(12, 6) NOT NULL DEFAULT 0,
    answer text,
    error text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS orchestrations_restaurant_idx
    ON orchestrations (restaurant_id, started_at);

ALTER TABLE runs ADD COLUMN IF NOT EXISTS orchestration_id text
    REFERENCES orchestrations ON DELETE CASCADE;
-- Which plan step (or "plan", "synthesis-1", "review-1") of the orchestration this run is.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS plan_step text;
-- The run started with write tools disabled (its question carried text derived from customer
-- reviews). A resume must keep them disabled.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS tainted boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS runs_orchestration_idx ON runs (orchestration_id);
