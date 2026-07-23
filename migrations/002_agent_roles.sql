DO $migration$
DECLARE
    app_password text := current_setting('omicsprism.app_db_password', true);
BEGIN
    IF app_password IS NULL OR app_password = '' THEN
        RAISE EXCEPTION 'omicsprism.app_db_password is required';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omics_app') THEN
        EXECUTE format(
            'CREATE ROLE omics_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
            app_password
        );
    ELSE
        EXECUTE format(
            'ALTER ROLE omics_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
            app_password
        );
    END IF;
END
$migration$;

GRANT USAGE ON SCHEMA public TO omics_app;

REVOKE ALL PRIVILEGES ON agent_runs FROM PUBLIC;
REVOKE ALL PRIVILEGES ON agent_events FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE ON agent_runs TO omics_app;
REVOKE DELETE ON agent_runs FROM omics_app;

GRANT SELECT, INSERT ON agent_events TO omics_app;
REVOKE UPDATE, DELETE ON agent_events FROM omics_app;
REVOKE TRUNCATE, REFERENCES, TRIGGER ON agent_events FROM omics_app;
