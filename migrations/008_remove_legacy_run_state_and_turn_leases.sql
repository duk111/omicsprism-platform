DROP INDEX IF EXISTS agent_runs_status_idx;
DROP INDEX IF EXISTS agent_turns_claim_idx;

ALTER TABLE agent_runs
    DROP COLUMN IF EXISTS active_profile,
    DROP COLUMN IF EXISTS state,
    DROP COLUMN IF EXISTS step_no,
    DROP COLUMN IF EXISTS model_calls,
    DROP COLUMN IF EXISTS tool_calls,
    DROP COLUMN IF EXISTS status;

ALTER TABLE agent_turns
    DROP COLUMN IF EXISTS lease_owner,
    DROP COLUMN IF EXISTS lease_expires_at;
