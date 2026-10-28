-- Audit rows carry the restaurant from the trusted run scope (not from model-chosen arguments)
-- and the tool_use_id that ties each row to one step of the conversation.
ALTER TABLE tool_audit ADD COLUMN IF NOT EXISTS restaurant_id integer;
ALTER TABLE tool_audit ADD COLUMN IF NOT EXISTS tool_use_id text;
CREATE INDEX IF NOT EXISTS tool_audit_restaurant_idx ON tool_audit (restaurant_id, created_at);
