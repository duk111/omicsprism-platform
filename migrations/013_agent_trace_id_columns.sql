ALTER TABLE agent_turns
    ADD COLUMN IF NOT EXISTS trace_id text;

UPDATE agent_turns
SET trace_id = 'trace-legacy-' || turn_id
WHERE trace_id IS NULL;

ALTER TABLE agent_turns
    ALTER COLUMN trace_id SET NOT NULL,
    ALTER COLUMN trace_id SET DEFAULT 'trace-local';

ALTER TABLE agent_messages
    ADD COLUMN IF NOT EXISTS trace_id text;

UPDATE agent_messages
SET trace_id = 'trace-legacy-' || message_id
WHERE trace_id IS NULL;

ALTER TABLE agent_messages
    ALTER COLUMN trace_id SET NOT NULL,
    ALTER COLUMN trace_id SET DEFAULT 'trace-local';

CREATE INDEX IF NOT EXISTS agent_turns_trace_idx
    ON agent_turns (trace_id, user_id, created_at);
CREATE INDEX IF NOT EXISTS agent_messages_trace_idx
    ON agent_messages (trace_id, user_id, created_at);
