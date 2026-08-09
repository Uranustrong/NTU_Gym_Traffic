-- Supabase-side upkeep for the two public RPCs: rebuild the heatmap cache on a
-- timer, and sweep the rate limiter's counters.
--
-- WHY THIS EXISTS
--
-- sql/migrations/005_public_api.sql makes public.gym_heatmap() read a stored
-- payload instead of recomputing a full-history aggregate on every request,
-- because that aggregate cost 510 ms of shared CPU per 6.5 KB response and the
-- functions are callable by anyone holding the publishable key printed in the
-- page. 005 is written so that neither job here is *required* -- a stale or
-- missing cache is rebuilt by the next caller, and the limiter garbage-collects
-- itself probabilistically -- because 005 also has to apply cleanly to the bare
-- postgres:17 container in the weekly restore drill, which has no pg_cron.
--
-- What these jobs buy is that the rebuild is paid by a scheduler at 07 past the
-- hour rather than by whichever visitor happens to arrive first after the cache
-- goes stale.
--
-- NOT A MIGRATION, for the same reason as 001: tools/apply_migrations.sh globs
-- sql/migrations/*.sql and the restore drill runs it against a container with
-- neither pg_cron nor pg_net. Applied by hand:
--
--   bash -c 'set -a; . ./supabase.secrets; set +a; \
--     psql "$SUPABASE_OWNER_URL" -X -v ON_ERROR_STOP=1 \
--       -f sql/supabase/002_cost_control_cron.sql'
--
-- Idempotent: cron.schedule() upserts by job name.

create extension if not exists pg_cron;

-- Hourly, matching the staleness threshold in gym_heatmap(). Offset off the
-- hour so it does not land on top of the collect dispatches at */5.
--
-- pg_cron schedules are UTC and cannot be given a timezone. That does not
-- matter here the way it does in 001 -- this job has no opening-hours logic,
-- it just runs 24 times a day -- so unlike the collect windows there is no
-- hand-shifted copy of the hours to keep in sync.
select cron.schedule('heatmap-cache-refresh', '7 * * * *',
    $job$select api_private.refresh_gym_heatmap_cache()$job$);

-- The limiter drops its own expired rows on ~1% of calls, which bounds the
-- table under normal traffic. This is the backstop for the case that argues
-- for a sweeper at all: a caller rotating source addresses inserts a row per
-- address, and rows only expire when someone else's call happens to trigger
-- the probabilistic sweep. Both tables are UNLOGGED, so this is reclaiming
-- memory-backed space, not disk.
select cron.schedule('rate-limit-gc', '17 * * * *',
    $job$delete from api_private.rate_limit_client
          where window_start < now() - interval '1 hour'$job$);

-- What ran, and whether it worked. Same shape as the check in 001.
select jobid, jobname, schedule, active
  from cron.job
 where jobname in ('heatmap-cache-refresh', 'rate-limit-gc')
 order by jobname;
