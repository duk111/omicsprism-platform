CREATE TABLE agent_job_waits (
    wait_id text PRIMARY KEY,
    thread_id text NOT NULL,
    user_id text NOT NULL,
    turn_id text NOT NULL,
    run_id text NOT NULL,
    trace_id text NOT NULL,
    job_id text NOT NULL,
    status text NOT NULL DEFAULT 'waiting' CHECK (
        status IN ('waiting', 'resume_queued', 'completed', 'failed', 'cancelled', 'expired')
    ),
    continuation_turn_id text,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, thread_id, user_id),
    FOREIGN KEY (thread_id, user_id)
        REFERENCES agent_threads (thread_id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id)
        REFERENCES agent_turns (turn_id) ON DELETE CASCADE
);

CREATE INDEX agent_job_waits_status_idx
    ON agent_job_waits (status, updated_at);
CREATE INDEX agent_job_waits_job_idx
    ON agent_job_waits (job_id, user_id);

CREATE TABLE agent_job_events (
    event_id text PRIMARY KEY,
    event_type text NOT NULL CHECK (event_type IN ('job.completed')),
    job_id text NOT NULL,
    thread_id text NOT NULL,
    user_id text NOT NULL,
    turn_id text NOT NULL,
    run_id text NOT NULL,
    trace_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('succeeded', 'failed', 'cancelled')),
    error_code text,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    delivery_attempts integer NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    last_error text,
    UNIQUE (job_id, status),
    FOREIGN KEY (thread_id, user_id)
        REFERENCES agent_threads (thread_id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id)
        REFERENCES agent_turns (turn_id) ON DELETE CASCADE
);

CREATE INDEX agent_job_events_pending_idx
    ON agent_job_events (published_at, created_at, event_id);
CREATE INDEX agent_job_events_job_idx
    ON agent_job_events (job_id, user_id, occurred_at);

REVOKE ALL PRIVILEGES ON agent_job_waits FROM PUBLIC;
REVOKE ALL PRIVILEGES ON agent_job_events FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON agent_job_waits TO omics_app;
GRANT SELECT, INSERT, UPDATE ON agent_job_events TO omics_app;
