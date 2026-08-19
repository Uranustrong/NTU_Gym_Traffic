-- 003: least-privilege roles for the GitHub Actions workflows.
--
-- Reads the two role passwords straight from the environment with \getenv, so
-- they never appear in the psql command line where `ps` could show them --
-- the same reason sync_to_postgres.py relies on PGPASSWORD rather than
-- embedding a password in the URL. Apply with an owner-level credential.
--
--   gym_writer  -> collect.yml.  SELECT + INSERT on occupancy, nothing else.
--   gym_reader  -> publish.yml, backup.yml (data-only pg_dump), local Grafana.
--                  Read-only.
--   owner       -> migrations and backfills that need DO UPDATE. Backups do
--                  NOT use it: they dump only public.occupancy as gym_reader.
--
-- Why gym_writer gets SELECT: sync_to_postgres.py verifies its own writes
-- inside the transaction by joining the staged rows against occupancy. The
-- data is published on a public page anyway, so read access costs nothing.
--
-- Why gym_writer does NOT get UPDATE: a leaked credential with UPDATE plus a
-- permissive policy could rewrite every historical count to zero. That is as
-- destructive as DELETE and just as silent. Routine collection only ever
-- appends, so ON CONFLICT DO NOTHING is sufficient.

\getenv gym_writer_password GYM_WRITER_PASSWORD
\getenv gym_reader_password GYM_READER_PASSWORD

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gym_writer') THEN
        CREATE ROLE gym_writer LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gym_reader') THEN
        CREATE ROLE gym_reader LOGIN;
    END IF;
END $$;

ALTER ROLE gym_writer PASSWORD :'gym_writer_password';
ALTER ROLE gym_reader PASSWORD :'gym_reader_password';

GRANT USAGE ON SCHEMA public TO gym_writer, gym_reader;

-- The TEMP staging table needs this. PUBLIC holds TEMPORARY by default on
-- stock PostgreSQL, but that is revoked on some hardened installs, so grant
-- it explicitly. The database name differs per environment (postgres on
-- Supabase, gym_fetch under docker compose), hence format().
DO $$
BEGIN
    EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO gym_writer',
                   current_database());
END $$;

GRANT SELECT, INSERT ON public.occupancy TO gym_writer;
GRANT USAGE, SELECT ON SEQUENCE public.occupancy_id_seq TO gym_writer;

GRANT SELECT ON public.occupancy TO gym_reader;
GRANT SELECT ON public.weekly_occupancy_slots TO gym_reader;
GRANT SELECT ON public.current_vs_history TO gym_reader;

-- The objects added with the closures table. The new views are
-- security_invoker (see 004), so Grafana reaching weekly_occupancy_slots now
-- also has to be able to read what that view reads.
GRANT SELECT ON public.closures TO gym_reader;
GRANT SELECT ON public.closure_periods TO gym_reader;
GRANT SELECT ON public.occupancy_excluding_closures TO gym_reader;

-- Needed by the backup, not by any query: pg_dump --data-only reads
-- occupancy_id_seq to record last_value, so a restore resumes the sequence
-- instead of colliding with existing ids. Without this the backup fails with
-- "permission denied for sequence occupancy_id_seq" -- and leaves a zero-byte
-- file behind that looks like a backup. Reading a sequence grants nothing
-- else; USAGE (which would allow consuming values) is deliberately withheld.
GRANT SELECT ON SEQUENCE public.occupancy_id_seq TO gym_reader;

-- Belt and braces: strip anything a previous, more permissive setup granted.
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON public.occupancy FROM gym_writer;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON public.occupancy FROM gym_reader;
