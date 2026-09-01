CREATE TABLE agent_trace_events (
    event_id text PRIMARY KEY,
    trace_id text NOT NULL,
    thread_id text NOT NULL,
    turn_id text,
    run_id text,
    user_id text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'turn.queued', 'turn.started', 'turn.completed', 'turn.failed',
        'model.call', 'tool.call', 'job.submitted'
    )),
    component text NOT NULL CHECK (component IN ('api', 'runtime', 'graph', 'model', 'tool', 'job')),
    name text NOT NULL,
    schema_version text NOT NULL,
    graph_version text NOT NULL,
    prompt_version text,
    prompt_hash text,
    model_provider text,
    model_name text,
    tool_name text,
    tool_schema_hash text,
    job_id text,
    outcome text,
    latency_ms double precision CHECK (latency_ms >= 0),
    prompt_tokens integer CHECK (prompt_tokens >= 0),
    completion_tokens integer CHECK (completion_tokens >= 0),
    total_tokens integer CHECK (total_tokens >= 0),
    cached_tokens integer CHECK (cached_tokens >= 0),
    usage_status text CHECK (usage_status IN ('reported', 'unknown')),
    retry_count integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (thread_id, user_id)
        REFERENCES agent_threads (thread_id, user_id)
);

CREATE INDEX agent_trace_events_trace_idx
    ON agent_trace_events (trace_id, user_id, created_at, event_id);
CREATE INDEX agent_trace_events_thread_idx
    ON agent_trace_events (thread_id, user_id, created_at);
CREATE INDEX agent_trace_events_type_idx
    ON agent_trace_events (event_type, created_at);

REVOKE ALL PRIVILEGES ON agent_trace_events FROM PUBLIC;
GRANT SELECT, INSERT, DELETE ON agent_trace_events TO omics_app;
