-- MCP server tables (pending human-confirmed actions, promotions, tool audit).
-- Idempotent: safe to re-apply on an existing volume.

CREATE TABLE IF NOT EXISTS pending_actions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    restaurant_id integer NOT NULL REFERENCES restaurants (id),
    kind text NOT NULL CHECK (kind IN ('pause_menu_item', 'create_promotion')),
    payload jsonb NOT NULL,
    reason text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'executed', 'rejected')),
    run_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_by text,
    decided_at timestamptz,
    decision_note text,
    -- A decision is recorded if and only if the action left 'pending'.
    CHECK ((status = 'pending') = (decided_at IS NULL)),
    CHECK ((status = 'pending') = (decided_by IS NULL))
);

CREATE INDEX IF NOT EXISTS pending_actions_status_idx ON pending_actions (status, created_at);

CREATE TABLE IF NOT EXISTS promotions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    restaurant_id integer NOT NULL REFERENCES restaurants (id),
    menu_item_id integer NOT NULL REFERENCES menu_items (id),
    discount_pct integer NOT NULL CHECK (discount_pct BETWEEN 1 AND 50),
    start_date date NOT NULL,
    end_date date NOT NULL,
    reason text NOT NULL,
    source_action_id bigint NOT NULL UNIQUE REFERENCES pending_actions (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (end_date >= start_date)
);

CREATE INDEX IF NOT EXISTS promotions_restaurant_idx ON promotions (restaurant_id, start_date);

-- No foreign keys on purpose: the audit log must accept rows for failed calls
-- (unknown restaurant, unknown tool) and outlive the data it describes.
CREATE TABLE IF NOT EXISTS tool_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text,
    tool text NOT NULL,
    args jsonb NOT NULL,
    result jsonb,
    error text,
    approved_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (result IS NULL OR error IS NULL)
);

CREATE INDEX IF NOT EXISTS tool_audit_run_idx ON tool_audit (run_id);
CREATE INDEX IF NOT EXISTS tool_audit_created_idx ON tool_audit (created_at);
