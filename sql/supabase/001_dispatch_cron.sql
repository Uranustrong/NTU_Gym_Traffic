-- Supabase-side scheduler: pg_cron wakes GitHub Actions instead of waiting for
-- GitHub's own scheduler to do it.
--
-- WHY THIS EXISTS
--
-- On 2026-08-09 the collect schedule delivered roughly one run every 22
-- minutes against a nominal 5, and the interval was not converging after an
-- hour of operation. Scheduled runs appeared to alternate between collect and
-- publish, which looks like a repository-level cap rather than either workflow
-- being at fault. Manual dispatches, by contrast, started with no delay every
-- single time -- four for four. So the throttle is on the `schedule` trigger,
-- not on Actions, and replacing only the trigger keeps every verified piece of
-- the pipeline exactly as it was: the venue-set check, the source-freshness
-- check, the VERIFY_X509_STRICT workaround, three sync retries, the in-
-- transaction row verification, the artifact on failure.
--
-- collect.yml keeps its own `schedule` block on purpose. Two triggers cannot
-- corrupt anything -- a duplicate (fetched_at, venue) hits ON CONFLICT DO
-- NOTHING, and a reading a couple of minutes off the grid is simply another
-- reading. What it buys is that the day this token expires, collection
-- degrades to GitHub's 20% rather than to zero.
--
-- NOT A MIGRATION. tools/apply_migrations.sh globs sql/migrations/*.sql and is
-- run against a bare postgres:17 container by the weekly restore drill, which
-- has neither pg_cron nor pg_net. This file is Supabase-instance
-- configuration and is applied by hand:
--
--   bash -c 'set -a; . ./supabase.secrets; set +a; \
--     psql "$SUPABASE_OWNER_URL" -X -v ON_ERROR_STOP=1 \
--       -f sql/supabase/001_dispatch_cron.sql'
--
-- Idempotent: re-running rotates the stored token and re-points the jobs.

\getenv gh_dispatch_token GITHUB_DISPATCH_TOKEN

\if :{?gh_dispatch_token}
\else
\echo 'error: GITHUB_DISPATCH_TOKEN is not set in the environment'
\quit 2
\endif

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Not in `public`: Supabase exposes public through PostgREST, and a function
-- that reads a GitHub token out of the vault has no business being reachable
-- over HTTP.
create schema if not exists ops;
revoke all on schema ops from public;

-- Vault stores it encrypted; vault.decrypted_secrets decrypts on read for
-- roles that can see it. The token never appears in a job definition, so
-- cron.job -- which is world-readable to anyone who can reach the cron schema
-- -- shows only a function call.
-- Two plain statements rather than one DO block, because psql does not
-- substitute :'variables' inside dollar-quoted strings -- the literal :'name'
-- would reach the server and fail to parse. Insert-if-absent, then always
-- update, so re-running the file rotates the token.
select vault.create_secret(
           :'gh_dispatch_token',
           'github_dispatch_token',
           'Fine-grained PAT, Actions:read+write on Uranustrong/NTU_Gym_Traffic. Read by ops.dispatch_workflow(). Expires 2026-11-07.')
 where not exists (select 1 from vault.secrets where name = 'github_dispatch_token');

select vault.update_secret(id, :'gh_dispatch_token')
  from vault.secrets
 where name = 'github_dispatch_token';

create or replace function ops.dispatch_workflow(workflow_file text)
returns bigint
language plpgsql
as $$
declare
    token text;
    request_id bigint;
begin
    select decrypted_secret into token
      from vault.decrypted_secrets
     where name = 'github_dispatch_token';

    if token is null then
        raise exception 'vault secret github_dispatch_token is missing';
    end if;

    -- pg_net is asynchronous: this returns as soon as the request is queued,
    -- and the outcome lands in net._http_response. A failure here is
    -- therefore silent by construction, which is what ops.dispatch_log is
    -- for. GitHub answers 204 with an empty body on success.
    select net.http_post(
        url := 'https://api.github.com/repos/Uranustrong/NTU_Gym_Traffic/actions/workflows/'
               || workflow_file || '/dispatches',
        headers := jsonb_build_object(
            'Accept', 'application/vnd.github+json',
            'Authorization', 'Bearer ' || token,
            'X-GitHub-Api-Version', '2022-11-28',
            'Content-Type', 'application/json',
            -- GitHub rejects API requests without one.
            'User-Agent', 'ntu-gym-traffic-pg-cron'
        ),
        body := jsonb_build_object('ref', 'main')
    ) into request_id;

    return request_id;
end;
$$;

revoke all on function ops.dispatch_workflow(text) from public;

-- How to tell whether the token still works. 204 is success; 401 means the
-- PAT expired or was revoked, 403 means its permissions were narrowed, 404
-- means it can no longer see the repository. Supabase prunes this table after
-- a few hours, so it answers "is it working now", not "what happened last
-- week".
create or replace view ops.dispatch_log as
select id,
       created,
       status_code,
       case status_code
           when 204 then 'ok'
           when 401 then 'TOKEN EXPIRED OR REVOKED'
           when 403 then 'token permissions too narrow'
           when 404 then 'token cannot see the repository'
           else 'unexpected'
       end as verdict,
       left(coalesce(content, ''), 300) as body,
       error_msg
  from net._http_response
 order by created desc;

-- ON TABLE, not ON VIEW: REVOKE has no VIEW variant, views are tables here.
revoke all on table ops.dispatch_log from public;

-- Schedules are UTC: pg_cron reads the database TimeZone, which is UTC on
-- Supabase and cannot be set per job. These are the same windows as
-- collect.yml's Taipei crons, shifted by -8 hours -- Taipei has no DST, so the
-- offset is fixed and the two definitions cannot drift apart seasonally.
--
--   Taipei Mon-Fri 06:00-07:55  ->  UTC Sun-Thu 22:00-23:55
--   Taipei Mon-Fri 08:00-21:55  ->  UTC Mon-Fri 00:00-13:55
--   Taipei Sat     09:00-21:55  ->  UTC Sat     01:00-13:55
--   Taipei Sun     09:00-17:55  ->  UTC Sun     01:00-09:55
--
-- Minutes are 0,5,...,55 here but 3,8,...,58 in collect.yml, and the asymmetry
-- is deliberate. The :03 offset exists to dodge GitHub's own scheduler, which
-- is documented to shed load at the top of every hour -- a constraint that
-- applies to the fallback trigger and not to this one, since pg_cron fires
-- within milliseconds of the tick and workflow_dispatch is never throttled.
-- Landing on multiples of five puts new readings back on the same grid as the
-- 13,014 of 13,020 pre-migration rows that sit at minute % 5 = 0. It also
-- staggers the two triggers, so a fallback run that does get through fills a
-- gap between ticks instead of duplicating one.
--
-- Over-firing is harmless: fetch_counts.py --open-hours-only is what actually
-- decides, and a closed-hours run exits 0 having written nothing.
select cron.schedule('collect-weekday-early',   '*/5 22-23 * * 0-4',
                     $job$select ops.dispatch_workflow('collect.yml')$job$);
select cron.schedule('collect-weekday-main',    '*/5 0-13 * * 1-5',
                     $job$select ops.dispatch_workflow('collect.yml')$job$);
select cron.schedule('collect-saturday',        '*/5 1-13 * * 6',
                     $job$select ops.dispatch_workflow('collect.yml')$job$);
select cron.schedule('collect-sunday',          '*/5 1-9 * * 0',
                     $job$select ops.dispatch_workflow('collect.yml')$job$);

-- publish rides the same windows two minutes behind, at 2,7,...,57.
--
-- The offset is the whole point. A render fired on the same tick as a
-- collection races it: the run takes about fifteen seconds to fetch, validate
-- and commit, so a publish starting at the same instant would read the table
-- just before the new row lands and ship a page that is a full cycle stale --
-- consistently, not occasionally. Two minutes is far more than the gap needs
-- to be and costs nothing.
--
-- Nothing extra is needed after closing time. The last collection of the day
-- is at :55 and this renders it at :57, so the page settles on the final
-- reading instead of the second-to-last one.
select cron.schedule('publish-weekday-early',   '2-57/5 22-23 * * 0-4',
                     $job$select ops.dispatch_workflow('publish.yml')$job$);
select cron.schedule('publish-weekday-main',    '2-57/5 0-13 * * 1-5',
                     $job$select ops.dispatch_workflow('publish.yml')$job$);
select cron.schedule('publish-saturday',        '2-57/5 1-13 * * 6',
                     $job$select ops.dispatch_workflow('publish.yml')$job$);
select cron.schedule('publish-sunday',          '2-57/5 1-9 * * 0',
                     $job$select ops.dispatch_workflow('publish.yml')$job$);

select jobid, jobname, schedule, active from cron.job order by jobname;
