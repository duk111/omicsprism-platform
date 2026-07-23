CREATE TABLE agent_runs (
    run_id text NOT NULL,
    user_id text NOT NULL,
    thread_id text NOT NULL,
    active_profile text NOT NULL CHECK (active_profile IN ('analysis', 'interpretation')),
    state text NOT NULL CHECK (
        state IN (
            'COLLECT_INTENT',
            'CHECK_INPUTS',
            'PROPOSE_PLAN',
            'WAIT_PLAN_CONFIRMATION',
            'PREFLIGHT',
            'WAIT_EXECUTION_CONFIRMATION',
            'SUBMIT_JOBS',
            'MONITOR_JOBS',
            'ANSWER_WITH_EVIDENCE',
            'AWAIT_FOLLOWUP',
            'DONE',
            'NEED_USER_INPUT',
            'PREFLIGHT_BLOCKED',
            'JOB_FAILED'
        )
    ),
    step_no integer NOT NULL CHECK (step_no >= 0),
    plan_id text,
    plan_hash text,
    pending_approval_id text,
    focus jsonb NOT NULL,
    model_calls integer NOT NULL CHECK (model_calls >= 0),
    tool_calls integer NOT NULL CHECK (tool_calls >= 0),
    status text NOT NULL CHECK (status IN ('running', 'suspended', 'completed', 'failed', 'cancelled')),
    version integer NOT NULL CHECK (version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, user_id)
);

CREATE INDEX agent_runs_user_thread_idx ON agent_runs (user_id, thread_id);
CREATE INDEX agent_runs_status_idx ON agent_runs (status, updated_at DESC);

CREATE TABLE agent_events (
    event_id text PRIMARY KEY,
    run_id text NOT NULL,
    user_id text NOT NULL,
    step_no integer NOT NULL CHECK (step_no >= 0),
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, user_id)
        REFERENCES agent_runs (run_id, user_id)
);

CREATE INDEX agent_events_run_idx ON agent_events (run_id, user_id, created_at);
CREATE INDEX agent_events_type_idx ON agent_events (event_type, created_at);
