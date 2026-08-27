ALTER TABLE agent_turns
    DROP CONSTRAINT IF EXISTS agent_turns_status_check;

ALTER TABLE agent_turns
    ADD CONSTRAINT agent_turns_status_check
    CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled'));

GRANT DELETE ON agent_threads, agent_messages, agent_turns,
    agent_input_bundles, agent_input_files TO omics_app;
