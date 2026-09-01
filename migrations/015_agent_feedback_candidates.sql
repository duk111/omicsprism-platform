CREATE TABLE agent_feedback (
    feedback_id text PRIMARY KEY,
    thread_id text NOT NULL,
    turn_id text NOT NULL,
    message_id text NOT NULL,
    trace_id text NOT NULL,
    user_id text NOT NULL,
    rating text NOT NULL CHECK (rating IN ('helpful', 'unhelpful')),
    failure_category text CHECK (failure_category IN (
        'incorrect_result', 'missing_context', 'bad_plan', 'unsafe_action', 'latency', 'other'
    )),
    correction_text text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (message_id, user_id),
    CHECK (
        (rating = 'helpful' AND failure_category IS NULL)
        OR (rating = 'unhelpful' AND failure_category IS NOT NULL)
    ),
    FOREIGN KEY (thread_id, user_id)
        REFERENCES agent_threads (thread_id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id)
        REFERENCES agent_turns (turn_id) ON DELETE CASCADE,
    FOREIGN KEY (message_id)
        REFERENCES agent_messages (message_id) ON DELETE CASCADE
);

CREATE INDEX agent_feedback_thread_idx
    ON agent_feedback (thread_id, user_id, updated_at, feedback_id);
CREATE INDEX agent_feedback_trace_idx
    ON agent_feedback (trace_id, user_id, updated_at);

CREATE TABLE agent_eval_candidates (
    candidate_id text PRIMARY KEY,
    feedback_id text NOT NULL UNIQUE REFERENCES agent_feedback (feedback_id) ON DELETE CASCADE,
    thread_id text NOT NULL,
    turn_id text NOT NULL,
    message_id text NOT NULL,
    trace_id text NOT NULL,
    user_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending_review', 'approved', 'rejected', 'withdrawn')),
    rating text NOT NULL CHECK (rating IN ('helpful', 'unhelpful')),
    failure_category text,
    user_message_summary text NOT NULL,
    assistant_message_summary text NOT NULL,
    correction_summary text,
    trace_summary jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (thread_id, user_id)
        REFERENCES agent_threads (thread_id, user_id) ON DELETE CASCADE
);

CREATE INDEX agent_eval_candidates_review_idx
    ON agent_eval_candidates (status, updated_at, candidate_id);

REVOKE ALL PRIVILEGES ON agent_feedback FROM PUBLIC;
REVOKE ALL PRIVILEGES ON agent_eval_candidates FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON agent_feedback TO omics_app;
GRANT SELECT, INSERT, UPDATE ON agent_eval_candidates TO omics_app;
