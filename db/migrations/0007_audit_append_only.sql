-- tool_audit is append-only: an audit row that can be edited proves nothing.
-- UPDATE is always refused. DELETE and TRUNCATE are refused unless the transaction opts in with
-- SET LOCAL brigada.audit_maintenance = 'on' (test cleanup and seed --reset; the app never does).
-- This stops mistakes and a compromised app path, not the database owner, who can drop the
-- trigger: for that, run the app under a role without UPDATE/DELETE on this table.
CREATE OR REPLACE FUNCTION tool_audit_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE'
       AND coalesce(current_setting('brigada.audit_maintenance', true), '') = 'on' THEN
        RETURN CASE WHEN TG_LEVEL = 'ROW' THEN OLD ELSE NULL END;
    END IF;
    RAISE EXCEPTION 'tool_audit is append-only (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS tool_audit_no_update_delete ON tool_audit;
CREATE TRIGGER tool_audit_no_update_delete
    BEFORE UPDATE OR DELETE ON tool_audit
    FOR EACH ROW EXECUTE FUNCTION tool_audit_append_only();

DROP TRIGGER IF EXISTS tool_audit_no_truncate ON tool_audit;
CREATE TRIGGER tool_audit_no_truncate
    BEFORE TRUNCATE ON tool_audit
    FOR EACH STATEMENT EXECUTE FUNCTION tool_audit_append_only();
