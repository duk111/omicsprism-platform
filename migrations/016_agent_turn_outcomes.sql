ALTER TABLE agent_turns
    ADD COLUMN outcome text CHECK (
        outcome IS NULL OR outcome IN (
            'completed', 'needs_input', 'unsupported', 'unresolved', 'failed'
        )
    ),
    ADD COLUMN failure_code text,
    ADD COLUMN attempted_roles jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (
            jsonb_typeof(attempted_roles) = 'array'
            AND jsonb_array_length(attempted_roles) <= 3
        );

UPDATE agent_turns
SET outcome = CASE
        WHEN status = 'failed' THEN 'failed'
        WHEN status = 'completed' THEN 'completed'
        ELSE NULL
    END,
    failure_code = CASE
        WHEN status <> 'failed' THEN NULL
        WHEN error_code = 'agent_turn_timeout' THEN 'model_unavailable'
        ELSE 'tool_execution_failed'
    END;

ALTER TABLE agent_turns DROP COLUMN error_code;
