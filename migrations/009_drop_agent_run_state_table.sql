-- Product tables retain run_id as an application correlation field, but the
-- legacy agent_runs table is no longer their state store. Remove only the
-- foreign keys that point at the legacy table before dropping it; CASCADE
-- would silently delete agent_threads, agent_turns and their user data.
DO $migration$
DECLARE
    constraint_row record;
BEGIN
    FOR constraint_row IN
        SELECT ns.nspname AS schema_name,
               cls.relname AS table_name,
               con.conname AS constraint_name
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace
        WHERE con.contype = 'f'
          AND con.confrelid = 'public.agent_runs'::regclass
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.%I DROP CONSTRAINT IF EXISTS %I',
            constraint_row.schema_name,
            constraint_row.table_name,
            constraint_row.constraint_name
        );
    END LOOP;
END
$migration$;

DROP TABLE IF EXISTS agent_events;
DROP TABLE IF EXISTS agent_runs;
