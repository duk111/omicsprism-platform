DROP TABLE agent_approvals;
DROP TABLE agent_plans;

ALTER TABLE agent_runs
    DROP COLUMN plan_id,
    DROP COLUMN plan_hash,
    DROP COLUMN pending_approval_id;
