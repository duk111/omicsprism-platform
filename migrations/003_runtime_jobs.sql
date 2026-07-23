CREATE TABLE IF NOT EXISTS jobs (
    id text PRIMARY KEY,
    project_id text,
    owner_type text NOT NULL DEFAULT 'user',
    owner_id text NOT NULL DEFAULT '',
    status text NOT NULL,
    analysis_type text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    payload jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_created_at_idx ON jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_owner_idx ON jobs (owner_type, owner_id);
CREATE INDEX IF NOT EXISTS jobs_deleted_at_idx ON jobs (deleted_at);

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM omics_app;
REVOKE ALL PRIVILEGES ON jobs FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE ON jobs TO omics_app;
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON jobs FROM omics_app;
