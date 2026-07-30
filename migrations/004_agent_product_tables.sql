CREATE TABLE agent_threads (
    thread_id text PRIMARY KEY,
    user_id text NOT NULL,
    title text NOT NULL,
    current_run_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'archived')),
    version integer NOT NULL CHECK (version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (thread_id, user_id),
    FOREIGN KEY (current_run_id, user_id)
        REFERENCES agent_runs (run_id, user_id)
);

CREATE INDEX agent_threads_user_updated_idx
    ON agent_threads (user_id, updated_at DESC, thread_id DESC);

CREATE TABLE agent_messages (
    message_id text PRIMARY KEY,
    thread_id text NOT NULL,
    run_id text NOT NULL,
    user_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    blocks jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (thread_id, user_id)
        REFERENCES agent_threads (thread_id, user_id),
    FOREIGN KEY (run_id, user_id)
        REFERENCES agent_runs (run_id, user_id)
);

CREATE INDEX agent_messages_thread_cursor_idx
    ON agent_messages (thread_id, user_id, created_at, message_id);

CREATE TABLE agent_turns (
    turn_id text PRIMARY KEY,
    thread_id text NOT NULL,
    run_id text NOT NULL,
    user_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    attempt integer NOT NULL CHECK (attempt >= 0),
    lease_owner text,
    lease_expires_at timestamptz,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    FOREIGN KEY (thread_id, user_id)
        REFERENCES agent_threads (thread_id, user_id),
    FOREIGN KEY (run_id, user_id)
        REFERENCES agent_runs (run_id, user_id)
);

CREATE UNIQUE INDEX agent_turns_user_idempotency_idx
    ON agent_turns (user_id, idempotency_key);

CREATE UNIQUE INDEX agent_turns_one_active_per_thread_idx
    ON agent_turns (thread_id, user_id)
    WHERE status IN ('queued', 'running');

CREATE INDEX agent_turns_claim_idx
    ON agent_turns (status, lease_expires_at, created_at);

CREATE TABLE agent_plans (
    plan_id text PRIMARY KEY,
    run_id text NOT NULL,
    thread_id text NOT NULL,
    user_id text NOT NULL,
    plan_hash text NOT NULL,
    payload jsonb NOT NULL,
    submitted_job_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    version integer NOT NULL CHECK (version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, user_id),
    FOREIGN KEY (thread_id, user_id)
        REFERENCES agent_threads (thread_id, user_id),
    FOREIGN KEY (run_id, user_id)
        REFERENCES agent_runs (run_id, user_id)
);

CREATE INDEX agent_plans_run_idx
    ON agent_plans (run_id, user_id, updated_at DESC);

CREATE TABLE agent_approvals (
    approval_id text PRIMARY KEY,
    plan_id text NOT NULL,
    run_id text NOT NULL,
    thread_id text NOT NULL,
    user_id text NOT NULL,
    plan_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (approval_id, user_id),
    FOREIGN KEY (plan_id, user_id)
        REFERENCES agent_plans (plan_id, user_id),
    FOREIGN KEY (thread_id, user_id)
        REFERENCES agent_threads (thread_id, user_id),
    FOREIGN KEY (run_id, user_id)
        REFERENCES agent_runs (run_id, user_id)
);

CREATE INDEX agent_approvals_run_idx
    ON agent_approvals (run_id, user_id, status, expires_at);

CREATE TABLE agent_input_bundles (
    bundle_id text PRIMARY KEY,
    thread_id text NOT NULL,
    user_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'consumed', 'expired')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bundle_id, user_id),
    FOREIGN KEY (thread_id, user_id)
        REFERENCES agent_threads (thread_id, user_id)
);

CREATE INDEX agent_input_bundles_expiry_idx
    ON agent_input_bundles (status, expires_at);

CREATE TABLE agent_input_files (
    file_id text PRIMARY KEY,
    bundle_id text NOT NULL,
    user_id text NOT NULL,
    field text NOT NULL CHECK (
        field IN ('counts', 'metadata', 'metabs', 'transcriptome', 'metabolome', 'group')
    ),
    filename text NOT NULL,
    storage_key text NOT NULL,
    checksum text NOT NULL,
    content_type text,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (bundle_id, user_id)
        REFERENCES agent_input_bundles (bundle_id, user_id),
    UNIQUE (bundle_id, field)
);

CREATE INDEX agent_input_files_bundle_idx
    ON agent_input_files (bundle_id, user_id, created_at);
