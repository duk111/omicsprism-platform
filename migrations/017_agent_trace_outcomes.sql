ALTER TABLE agent_trace_events
    RENAME COLUMN error_code TO failure_code;

ALTER TABLE agent_trace_events
    ADD COLUMN attempted_roles jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (
            jsonb_typeof(attempted_roles) = 'array'
            AND jsonb_array_length(attempted_roles) <= 3
        );
