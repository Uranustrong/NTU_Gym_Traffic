-- 004: keep the data off Supabase's public REST API.
--
-- Supabase exposes every table in `public` through PostgREST, and the anon key
-- that reaches it is public by design. Without this file, anyone who reads the
-- key out of a page could query -- or write -- occupancy directly.
--
-- Enabling RLS on the table alone is NOT enough. Both views are owned by a
-- high-privilege role, and PostgreSQL defaults views to security_invoker =
-- off, meaning they execute as their owner and see straight past the
-- underlying table's RLS. The views have to be switched to invoker rights or
-- they become the bypass.
--
-- Nothing here restricts our own access: a table owner bypasses RLS unless
-- FORCE ROW LEVEL SECURITY is set, which it deliberately is not.

ALTER VIEW public.weekly_occupancy_slots SET (security_invoker = true);
ALTER VIEW public.current_vs_history     SET (security_invoker = true);

ALTER TABLE public.occupancy ENABLE ROW LEVEL SECURITY;

-- anon/authenticated only exist on Supabase; skip cleanly on local Postgres.
DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'REVOKE ALL ON public.occupancy, '
                'public.weekly_occupancy_slots, '
                'public.current_vs_history FROM %I', role_name);
            EXECUTE format(
                'REVOKE ALL ON SEQUENCE public.occupancy_id_seq FROM %I',
                role_name);
        END IF;
    END LOOP;
END $$;

-- With RLS on, non-owner roles need explicit policies. Roles come from 003.
DROP POLICY IF EXISTS gym_writer_insert ON public.occupancy;
DROP POLICY IF EXISTS gym_writer_select ON public.occupancy;
DROP POLICY IF EXISTS gym_reader_select ON public.occupancy;

-- Bound the blast radius of a leaked collector credential: it can only write
-- readings dated around now, never rewrite history. The small forward margin
-- absorbs clock skew between the runner and the database.
CREATE POLICY gym_writer_insert ON public.occupancy
    FOR INSERT TO gym_writer
    WITH CHECK (fetched_at >  now() - interval '1 hour'
            AND fetched_at <= now() + interval '5 minutes');

-- Needed by the in-transaction write verification in sync_to_postgres.py.
CREATE POLICY gym_writer_select ON public.occupancy
    FOR SELECT TO gym_writer USING (true);

CREATE POLICY gym_reader_select ON public.occupancy
    FOR SELECT TO gym_reader USING (true);
