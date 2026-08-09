-- 005: the read-only API the public page calls straight from the browser.
--
-- Before this, tools/render_public_html.py ran eight panel queries and baked
-- the results into index.html, so the page could only be as fresh as its last
-- deploy. Now the browser calls two functions and the page is a static shell.
--
-- TWO LAYERS, AND THE SPLIT IS THE POINT
--
--   api_private.gym_panel*_rows()   canonical relational primitives.
--                                   The single definition of each panel's SQL.
--                                   Grafana selects from these too.
--   public.gym_heatmap()            JSON wrappers for the browser. The only
--   public.gym_live()               things PostgREST can see.
--
-- PostgREST exposes `public` and nothing else, so putting the primitives in
-- api_private means anon gets a 404 on them rather than a 403 -- invisible
-- beats forbidden, and it makes a future accidental GRANT harmless.
--
-- The wrappers are SECURITY DEFINER so anon needs no privilege on occupancy
-- and no RLS policy of its own; 004 stays exactly as it was, with anon and
-- authenticated holding nothing on the table or either view. The primitives
-- are deliberately INVOKER: called through a wrapper they run as its owner,
-- and called directly by gym_reader from Grafana they run as gym_reader,
-- which is what its RLS policy expects.
--
-- Every function pins search_path to '' and fully qualifies its relations,
-- which is what makes SECURITY DEFINER safe to use here.

CREATE SCHEMA IF NOT EXISTS api_private;
REVOKE ALL ON SCHEMA api_private FROM PUBLIC;

-- The only index occupancy had was the unique (fetched_at, venue) that drives
-- ON CONFLICT. Both of the queries on the five-minute path filter the other way
-- round -- panel 3 by venue over a time range, panel 1's marker by DISTINCT ON
-- (venue) ORDER BY fetched_at DESC -- so they were reading through the whole
-- table. Measured on the live database, three runs each: gym_live(7) takes
-- 184 ms without this index and 127 ms with it. It does nothing for
-- gym_heatmap (508 ms vs 492 ms), whose cost is the full-table aggregate
-- behind weekly_occupancy_slots and which no venue-range index can help.
--
-- Worth having because that 31% is on the call every open tab makes every five
-- minutes, and it is also the cheapest thing standing between a public RPC and
-- someone calling it in a loop.
CREATE INDEX IF NOT EXISTS ix_occupancy_venue_fetched_at
    ON public.occupancy (venue, fetched_at DESC);

-- ---------------------------------------------------------------------------
-- Cost control
-- ---------------------------------------------------------------------------
-- The two wrappers are unauthenticated compute on a Free plan, and the risk is
-- quota rather than disclosure -- the data is public by design. Measured on the
-- live database before any of this existed:
--
--   gym_heatmap()   6.5 KB gzip on the wire, 510 ms of server CPU
--   gym_live(7)     8.2 KB gzip on the wire, 160 ms of server CPU
--
-- Legitimate traffic is nowhere near the 5 GB/month egress ceiling: a visit
-- costs ~15 KB, so the quota is ~360k visits, and a tab left open around the
-- clock costs 67 MB/month. A loop is a different matter. One single-threaded
-- `while true; do curl; done` sustains ~22 KB/s against gym_live, which is
-- 1.9 GB/day -- the monthly egress in 2.6 days. CPU goes first, though: at
-- 510 ms of work per 700 ms round trip, that one loop already occupies ~73% of
-- a core on an instance whose CPU is shared, and the collector's COPY (175 ms)
-- then has to compete for it. Egress exhaustion at least sends mail; CPU
-- starvation just quietly drops readings, which are not recoverable.
--
-- Pushing the cache to the edge would have been the cheap answer and it is not
-- available: Supabase's Cloudflare returns cf-cache-status: DYNAMIC for
-- /rest/v1/rpc/* even when the function emits Cache-Control: public, max-age.
-- Verified against this project. So everything has to be handled in-database,
-- which is what the rest of this section and the heatmap cache below do.

-- Tunable without a migration: hand-edit the row and it survives, because the
-- seed below never overwrites an existing bucket.
CREATE TABLE IF NOT EXISTS api_private.rate_limit_policy (
    bucket         text PRIMARY KEY,
    per_client     integer NOT NULL,  -- calls one client may make per window
    window_secs    integer NOT NULL,
    per_client_day integer NOT NULL,  -- ... and per day, so one cannot take it all
    daily_global   integer NOT NULL   -- calls everyone together may make per day
);

-- Three limits, because they defend three different things, and the middle one
-- exists because of NTU specifically.
--
-- per_client stops a runaway loop within seconds of it starting. It cannot be
-- tight, because campus wifi is NAT: a few hundred students share one public
-- address, so a limit sized for "one person" would lock out a lecture theatre.
-- At 60/min this allows ~300 tabs behind one address (an open tab polls
-- gym_live 12 times an hour) while still catching a loop almost immediately --
-- an unthrottled loop sustains around 160/min.
--
-- per_client_day bounds what any single address can take of the day's budget.
-- Without it the generous per-minute limit hands one caller the whole global
-- allowance before lunchtime, and the page goes dark for everyone else. 3000 of
-- 8000 is roughly a third: enough that a busy NAT is never the one blocked,
-- little enough that an abuser leaves most of the day intact for real viewers.
--
-- daily_global is what actually bounds the monthly bill. 8000 + 4000 calls/day
-- at the sizes above is ~92 MB/day, ~2.8 GB/month: inside the 5 GB ceiling even
-- if every one of those calls came from an abuser, and roughly a thousand times
-- present demand.
--
-- The residual tradeoff is stated rather than hidden: a determined caller
-- spreading itself across many addresses can still spend the global budget, and
-- the page then degrades for everyone until 00:00 UTC. It degrades to cached
-- data, which the page already handles, and collection, Grafana and backups are
-- untouched. Losing the page for an afternoon beats losing the project.
--
-- Every number here is tunable with an UPDATE. The seed never overwrites a row
-- that already exists, so hand-tuning survives re-running this migration.
INSERT INTO api_private.rate_limit_policy (bucket, per_client, window_secs, per_client_day, daily_global)
VALUES ('gym_live',    60, 60, 3000, 8000),
       ('gym_heatmap', 30, 60, 1500, 4000)
ON CONFLICT (bucket) DO NOTHING;

-- UNLOGGED on purpose, both of them: this is counters, not records. It keeps
-- the write-per-request out of the WAL entirely, and a restart clearing the
-- counters fails open, which is the right direction for a limiter guarding a
-- hobby project's quota.
-- One row per client per bucket carrying both counters, rolled in place rather
-- than one row per window, so the table is bounded by distinct callers instead
-- of growing with time -- and so both limits cost a single upsert.
CREATE UNLOGGED TABLE IF NOT EXISTS api_private.rate_limit_client (
    bucket       text NOT NULL,
    client       text NOT NULL,
    window_start timestamptz NOT NULL,
    hits         integer NOT NULL,
    day          date NOT NULL,
    day_hits     integer NOT NULL,
    PRIMARY KEY (bucket, client)
);
-- For the garbage collector, which sweeps by window rather than by client.
CREATE INDEX IF NOT EXISTS ix_rate_limit_client_window
    ON api_private.rate_limit_client (window_start);

CREATE UNLOGGED TABLE IF NOT EXISTS api_private.rate_limit_global (
    bucket text NOT NULL,
    day    date NOT NULL,
    hits   bigint NOT NULL,
    PRIMARY KEY (bucket, day)
);

CREATE OR REPLACE FUNCTION api_private.rate_limit_check(p_bucket text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SET search_path = ''
AS $$
DECLARE
    pol      api_private.rate_limit_policy;
    who      text;
    win      timestamptz;
    today    date;
    n        integer;
    nd       integer;
    g        bigint;
BEGIN
    SELECT * INTO pol FROM api_private.rate_limit_policy p WHERE p.bucket = p_bucket;
    IF NOT FOUND THEN
        RETURN;   -- an unconfigured bucket is not a limited one
    END IF;

    -- Only HTTP callers are limited. request.headers is set by PostgREST and
    -- is absent on a direct libpq connection, so migrations, psql, pg_cron and
    -- Grafana are all exempt without needing an allowlist -- and none of them
    -- is anonymous in the first place. cf-connecting-ip is Cloudflare's, and
    -- is present on this project; x-forwarded-for is the fallback.
    who := coalesce(
        nullif(current_setting('request.headers', true), '')::json ->> 'cf-connecting-ip',
        nullif(current_setting('request.headers', true), '')::json ->> 'x-forwarded-for');
    IF who IS NULL THEN
        RETURN;
    END IF;

    win := to_timestamp(
        floor(extract(epoch FROM clock_timestamp()) / pol.window_secs) * pol.window_secs);
    today := (clock_timestamp() AT TIME ZONE 'UTC')::date;

    -- Both client-side counters in one upsert. Each rolls independently: the
    -- window counter resets every window_secs, the daily one at 00:00 UTC.
    INSERT INTO api_private.rate_limit_client AS c
        (bucket, client, window_start, hits, day, day_hits)
    VALUES (p_bucket, who, win, 1, today, 1)
    ON CONFLICT (bucket, client) DO UPDATE
       SET hits         = CASE WHEN c.window_start = win   THEN c.hits + 1     ELSE 1 END,
           window_start = win,
           day_hits     = CASE WHEN c.day = today THEN c.day_hits + 1 ELSE 1 END,
           day          = today
    RETURNING c.hits, c.day_hits INTO n, nd;

    -- Cheap and rare rather than scheduled, so the limiter needs no companion
    -- cron job to stay bounded. sql/supabase/002_heatmap_refresh_cron.sql adds
    -- one anyway; this is what keeps a bare container honest.
    IF random() < 0.01 THEN
        DELETE FROM api_private.rate_limit_client
         WHERE window_start < clock_timestamp() - interval '1 hour';
        DELETE FROM api_private.rate_limit_global
         WHERE day < (clock_timestamp() AT TIME ZONE 'UTC')::date - 7;
    END IF;

    -- PostgREST turns a PTnnn SQLSTATE into HTTP nnn, so this reaches the
    -- browser as a 429 rather than a 500. Raising also rolls the increment
    -- back, which is exactly right: the counter stays pinned at the limit for
    -- the rest of the window instead of running away, and a refused call
    -- consumes no global budget because it produced no egress either.
    IF n > pol.per_client THEN
        RAISE SQLSTATE 'PT429' USING
            MESSAGE = format('rate limit: %s calls per %s seconds per client',
                             pol.per_client, pol.window_secs),
            HINT    = 'This data is cached by the page; it does not need to be polled faster.';
    END IF;

    IF nd > pol.per_client_day THEN
        RAISE SQLSTATE 'PT429' USING
            MESSAGE = format('rate limit: %s calls per day per client', pol.per_client_day),
            HINT    = 'One caller may not spend the whole project''s daily budget.';
    END IF;

    INSERT INTO api_private.rate_limit_global AS gl (bucket, day, hits)
    VALUES (p_bucket, (clock_timestamp() AT TIME ZONE 'UTC')::date, 1)
    ON CONFLICT (bucket, day) DO UPDATE SET hits = gl.hits + 1
    RETURNING gl.hits INTO g;

    IF g > pol.daily_global THEN
        RAISE SQLSTATE 'PT429' USING
            MESSAGE = format('daily budget for %s exhausted (%s calls)',
                             p_bucket, pol.daily_global),
            HINT    = 'Serving this from a free-tier project; the budget resets at 00:00 UTC.';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Opening hours, once
-- ---------------------------------------------------------------------------
-- Both the panel-3 filter and the staleness signal need to know whether the
-- centre is open right now, and the page must not decide that for itself: the
-- viewer's browser may be in any timezone, and a JS copy would be one more
-- place for the hours to drift out of sync.
CREATE OR REPLACE FUNCTION api_private.gym_is_open(p_at timestamptz DEFAULT now())
RETURNS boolean
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    SELECT CASE s.dow
               WHEN 6 THEN s.hr >= 9 AND s.hr < 22   -- Saturday
               WHEN 7 THEN s.hr >= 9 AND s.hr < 18   -- Sunday
               ELSE        s.hr >= 6 AND s.hr < 22   -- Mon-Fri, 06:00 not 08:00
           END
    FROM (SELECT EXTRACT(ISODOW FROM p_at AT TIME ZONE 'Asia/Taipei')::int AS dow,
                 EXTRACT(HOUR   FROM p_at AT TIME ZONE 'Asia/Taipei')::int AS hr) s;
$$;

-- ---------------------------------------------------------------------------
-- Panel 1 -- Popular Times. Three primitives, not one.
-- ---------------------------------------------------------------------------
-- Split because only the "now" marker belongs on the five-minute path. Left as
-- a single function with a UNION, the planner gives no guarantee that
-- filtering kind = 'now' outside would prune the historical CTEs, so the
-- heatmap-sized aggregate would run on every poll. Separate functions make
-- that structural instead of hopeful.

CREATE OR REPLACE FUNCTION api_private.gym_panel1_typical_rows(p_venue text)
RETURNS TABLE (hour integer, typical_count integer, samples bigint)
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    WITH today AS (
        SELECT EXTRACT(ISODOW FROM now() AT TIME ZONE 'Asia/Taipei')::int AS wd
    )
    SELECT EXTRACT(HOUR FROM w.slot_start_time)::int,
           ROUND(AVG(w.avg_current_count))::int,
           SUM(w.sample_count)
    FROM public.weekly_occupancy_slots w, today
    WHERE w.venue = p_venue
      AND w.weekday_number = today.wd
    GROUP BY 1;
$$;

CREATE OR REPLACE FUNCTION api_private.gym_panel1_now_rows(p_venue text)
RETURNS TABLE (hour integer, current_count integer)
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    SELECT DISTINCT ON (o.venue)
           EXTRACT(HOUR FROM o.fetched_at AT TIME ZONE 'Asia/Taipei')::int,
           o.current_count
    FROM public.occupancy o
    WHERE o.venue = p_venue
      AND o.fetched_at >= now() - interval '30 minutes'
    ORDER BY o.venue, o.fetched_at DESC;
$$;

CREATE OR REPLACE FUNCTION api_private.gym_panel1_meta_rows(p_venue text)
RETURNS TABLE (n integer)
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    WITH today AS (
        SELECT EXTRACT(ISODOW FROM now() AT TIME ZONE 'Asia/Taipei')::int AS wd
    )
    SELECT count(DISTINCT (o.fetched_at AT TIME ZONE 'Asia/Taipei')::date)::int
    FROM public.occupancy o, today
    WHERE o.venue = p_venue
      AND EXTRACT(ISODOW FROM o.fetched_at AT TIME ZONE 'Asia/Taipei')::int = today.wd;
$$;

-- ---------------------------------------------------------------------------
-- Panel 2 -- Weekly heatmap
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION api_private.gym_panel2_rows(p_venue text, p_gran text)
RETURNS TABLE (weekday_number integer, slot_label text, slot_start_time time,
               avg_count numeric, crowding_ratio numeric, sample_count bigint)
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    WITH bucketed AS (
        SELECT w.weekday_number AS wd,
               CASE WHEN p_gran = '60min'
                    THEN to_char(date_trunc('hour', ('2000-01-01 '||w.slot_start_time)::timestamp), 'HH24:00')
                    ELSE w.slot_label END AS lbl,
               CASE WHEN p_gran = '60min'
                    THEN date_trunc('hour', ('2000-01-01 '||w.slot_start_time)::timestamp)::time
                    ELSE w.slot_start_time END AS sst,
               w.avg_current_count, w.avg_crowding_ratio, w.sample_count
        FROM public.weekly_occupancy_slots w
        WHERE w.venue = p_venue
    )
    SELECT b.wd, b.lbl, b.sst,
           AVG(b.avg_current_count),
           AVG(b.avg_crowding_ratio),
           SUM(b.sample_count)
    FROM bucketed b
    GROUP BY b.wd, b.lbl, b.sst
    ORDER BY b.sst, b.wd;
$$;

-- ---------------------------------------------------------------------------
-- Panel 3 -- raw readings
-- ---------------------------------------------------------------------------
-- Takes a time range rather than a day count, because this is the one place
-- where the two callers genuinely differed: Grafana passes the dashboard's
-- time picker through $__timeFilter and wants a TIMESTAMPTZ back, while the
-- public page wanted `now() - N days` and epoch milliseconds. A p_days
-- signature would have silently broken Grafana's previous-week and custom
-- ranges. The narrower caller adapts; the canonical function stays general,
-- and the epoch conversion happens in the JSON wrapper where it belongs.
CREATE OR REPLACE FUNCTION api_private.gym_panel3_rows(
    p_venue text, p_from timestamptz, p_to timestamptz)
RETURNS TABLE ("time" timestamptz, metric text, value integer)
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    SELECT o.fetched_at, o.venue, o.current_count
    FROM public.occupancy o
    WHERE o.venue = p_venue
      AND o.fetched_at >= p_from
      AND o.fetched_at <= p_to
      AND api_private.gym_is_open(o.fetched_at)
    ORDER BY o.fetched_at;
$$;

-- ---------------------------------------------------------------------------
-- Panel 4 -- live cards
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION api_private.gym_panel4_rows()
RETURNS TABLE (kind text, venue text, ts_ms numeric, current_count integer,
               hist_median numeric, diff numeric, ratio numeric,
               comfortable integer, capacity integer)
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    WITH last_day AS (
        SELECT max((o.fetched_at AT TIME ZONE 'Asia/Taipei')::date) AS d
        FROM public.occupancy o
    ),
    latest_meta AS (
        SELECT DISTINCT ON (o.venue)
               o.venue AS v, o.comfortable_count AS comf, o.capacity_count AS cap
        FROM public.occupancy o
        WHERE o.comfortable_count IS NOT NULL OR o.capacity_count IS NOT NULL
        ORDER BY o.venue, o.fetched_at DESC
    ),
    readings_recent AS (
        SELECT o.venue AS v, o.fetched_at AS ts, o.current_count AS cnt
        FROM public.occupancy o, last_day
        WHERE (o.fetched_at AT TIME ZONE 'Asia/Taipei')::date = last_day.d
    )
    SELECT 'summary'::text, cvh.venue,
           EXTRACT(EPOCH FROM cvh.latest_fetched_at) * 1000,
           cvh.current_count,
           cvh.historical_median_current_count,
           cvh.difference_from_median,
           cvh.current_crowding_ratio,
           m.comf, m.cap
    FROM public.current_vs_history cvh
    LEFT JOIN latest_meta m ON m.v = cvh.venue
    UNION ALL
    SELECT 'reading'::text, r.v,
           EXTRACT(EPOCH FROM r.ts) * 1000,
           r.cnt, NULL, NULL, NULL, NULL, NULL
    FROM readings_recent r
    ORDER BY 1 DESC, 2, 3;
$$;

-- ---------------------------------------------------------------------------
-- Grafana series shape
-- ---------------------------------------------------------------------------
-- The browser reuses the ECharts panel code lifted verbatim out of
-- weekly-dashboard.json, which expects Grafana's frame shape:
--   {"fields": [{"name": ..., "type": ..., "values": [...]}]}
-- Building it here rather than in JS keeps the type metadata next to the query
-- that produces it, and means the page needs no equivalent of the Python
-- to_series()/_coerce() pair.
CREATE OR REPLACE FUNCTION api_private.series_field(
    p_name text, p_type text, p_values jsonb)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SET search_path = ''
AS $$
    -- coalesce keeps the empty case as [] rather than null, matching what the
    -- Python renderer always emitted.
    SELECT jsonb_build_object('name', p_name, 'type', p_type,
                              'values', coalesce(p_values, '[]'::jsonb));
$$;

-- Every jsonb_agg below carries its own ORDER BY over an explicit row_number.
-- The fields are parallel arrays read by index, so they must agree on order;
-- relying on the order a CTE happens to emit would be a silent misalignment
-- waiting to happen.

CREATE OR REPLACE FUNCTION api_private.gym_panel1_series(
    p_venue text, p_include_history boolean, p_include_now boolean)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    WITH rows AS (
        SELECT 'typical'::text AS kind, t.hour, t.typical_count AS count, t.samples
        FROM api_private.gym_panel1_typical_rows(p_venue) t
        WHERE p_include_history
        UNION ALL
        SELECT 'now'::text, n.hour, n.current_count, 1::bigint
        FROM api_private.gym_panel1_now_rows(p_venue) n
        WHERE p_include_now
        UNION ALL
        SELECT 'meta'::text, NULL::int, m.n, NULL::bigint
        FROM api_private.gym_panel1_meta_rows(p_venue) m
        WHERE p_include_history
    ),
    ord AS (
        SELECT r.*, row_number() OVER (ORDER BY r.kind DESC, r.hour NULLS LAST) AS ord
        FROM rows r
    )
    SELECT jsonb_build_object('fields', jsonb_build_array(
        api_private.series_field('kind',   'string', jsonb_agg(to_jsonb(o.kind)   ORDER BY o.ord)),
        api_private.series_field('hour',   'number', jsonb_agg(to_jsonb(o.hour)   ORDER BY o.ord)),
        api_private.series_field('count',  'number', jsonb_agg(to_jsonb(o.count)  ORDER BY o.ord)),
        api_private.series_field('samples','number', jsonb_agg(to_jsonb(o.samples) ORDER BY o.ord))
    ))
    FROM ord o;
$$;

CREATE OR REPLACE FUNCTION api_private.gym_panel2_series(p_venue text, p_gran text)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    WITH ord AS (
        SELECT p.*, row_number() OVER (ORDER BY p.slot_start_time, p.weekday_number) AS ord
        FROM api_private.gym_panel2_rows(p_venue, p_gran) p
    )
    SELECT jsonb_build_object('fields', jsonb_build_array(
        api_private.series_field('weekday_number', 'number', jsonb_agg(to_jsonb(o.weekday_number) ORDER BY o.ord)),
        api_private.series_field('slot_label',     'string', jsonb_agg(to_jsonb(o.slot_label) ORDER BY o.ord)),
        -- to_char, not to_jsonb: a `time` renders as "14:30:00" through jsonb
        -- anyway, but stating it keeps the contract independent of that.
        api_private.series_field('slot_start_time','string', jsonb_agg(to_jsonb(to_char(o.slot_start_time, 'HH24:MI:SS')) ORDER BY o.ord)),
        api_private.series_field('avg_count',      'number', jsonb_agg(to_jsonb(o.avg_count) ORDER BY o.ord)),
        api_private.series_field('crowding_ratio', 'number', jsonb_agg(to_jsonb(o.crowding_ratio) ORDER BY o.ord)),
        api_private.series_field('sample_count',   'number', jsonb_agg(to_jsonb(o.sample_count) ORDER BY o.ord))
    ))
    FROM ord o;
$$;

CREATE OR REPLACE FUNCTION api_private.gym_panel3_series(
    p_venue text, p_from timestamptz, p_to timestamptz)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    WITH ord AS (
        SELECT p.*, row_number() OVER (ORDER BY p."time") AS ord
        FROM api_private.gym_panel3_rows(p_venue, p_from, p_to) p
    )
    SELECT jsonb_build_object('fields', jsonb_build_array(
        -- epoch milliseconds here, TIMESTAMPTZ in the relational function:
        -- this is the one conversion the public page needs and Grafana does not.
        api_private.series_field('time',   'time',   jsonb_agg(to_jsonb(EXTRACT(EPOCH FROM o."time") * 1000) ORDER BY o.ord)),
        api_private.series_field('metric', 'string', jsonb_agg(to_jsonb(o.metric) ORDER BY o.ord)),
        api_private.series_field('value',  'number', jsonb_agg(to_jsonb(o.value)  ORDER BY o.ord))
    ))
    FROM ord o;
$$;

CREATE OR REPLACE FUNCTION api_private.gym_panel4_series()
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    WITH ord AS (
        SELECT p.*, row_number() OVER (ORDER BY p.kind DESC, p.venue, p.ts_ms) AS ord
        FROM api_private.gym_panel4_rows() p
    )
    SELECT jsonb_build_object('fields', jsonb_build_array(
        api_private.series_field('kind',          'string', jsonb_agg(to_jsonb(o.kind)          ORDER BY o.ord)),
        api_private.series_field('venue',         'string', jsonb_agg(to_jsonb(o.venue)         ORDER BY o.ord)),
        api_private.series_field('ts_ms',         'number', jsonb_agg(to_jsonb(o.ts_ms)         ORDER BY o.ord)),
        api_private.series_field('current_count', 'number', jsonb_agg(to_jsonb(o.current_count) ORDER BY o.ord)),
        api_private.series_field('hist_median',   'number', jsonb_agg(to_jsonb(o.hist_median)   ORDER BY o.ord)),
        api_private.series_field('diff',          'number', jsonb_agg(to_jsonb(o.diff)          ORDER BY o.ord)),
        api_private.series_field('ratio',         'number', jsonb_agg(to_jsonb(o.ratio)         ORDER BY o.ord)),
        api_private.series_field('comfortable',   'number', jsonb_agg(to_jsonb(o.comfortable)   ORDER BY o.ord)),
        api_private.series_field('capacity',      'number', jsonb_agg(to_jsonb(o.capacity)      ORDER BY o.ord))
    ))
    FROM ord o;
$$;

-- ---------------------------------------------------------------------------
-- The two things PostgREST can see
-- ---------------------------------------------------------------------------
-- Both carry the same freshness header:
--   dataAsOf          newest reading in the table
--   servedAt          database clock
--   freshnessExpected whether the centre is open right now, in Taipei
-- The page must judge staleness from dataAsOf, never from "the request
-- succeeded". A stopped collector still answers 200 -- viewers polling the API
-- are themselves enough to keep a Free project awake -- so a page that trusted
-- the response would quietly re-cache old numbers as if they were new.

-- Both wrappers are VOLATILE, which they would not need to be on their own
-- merits. A STABLE function may not write, and the rate limiter counts -- so
-- the choice is between counting and staying read-only. Two things make this
-- cheaper than it looks: PostgREST still accepts POST (only its GET form is
-- restricted to STABLE functions, and the page has always used POST), and the
-- bodies are fixed SQL running as their owner with search_path pinned to '',
-- so a read-write transaction gives a caller no reach it did not already have.

-- ---------------------------------------------------------------------------
-- The heatmap is computed on a schedule, not on request
-- ---------------------------------------------------------------------------
-- gym_heatmap's 510 ms was entirely the full-history aggregate behind
-- weekly_occupancy_slots, which the venue/time index cannot help and which
-- changes meaningfully about once a day -- it is a multi-month average, so five
-- more minutes of readings move nothing a viewer can see. Recomputing it per
-- request was handing an abuser half a CPU-second per 6.5 KB request, the worst
-- exchange rate anywhere in this project.
--
-- So the body is built on a timer and stored. The wrapper reads one row.
--
-- Deliberately LOGGED, unlike the rate-limit counters: it is one row, and
-- losing it to a restart costs a 510 ms rebuild rather than nothing.
CREATE TABLE IF NOT EXISTS api_private.gym_heatmap_cache (
    only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
    built_at timestamptz NOT NULL DEFAULT now(),
    payload  jsonb NOT NULL
);

-- The expensive part, kept separate so both the refresher and the cold-cache
-- path can call it. No freshness header here: that has to be current, so the
-- wrapper adds it after reading the cache.
CREATE OR REPLACE FUNCTION api_private.gym_heatmap_body()
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    WITH v AS (
        SELECT DISTINCT o.venue FROM public.occupancy o ORDER BY o.venue
    )
    SELECT jsonb_build_object(
        'venues', coalesce((SELECT jsonb_agg(v.venue ORDER BY v.venue) FROM v), '[]'::jsonb),
        'panel2', coalesce((SELECT jsonb_object_agg(v.venue, jsonb_build_object(
                       '30min', api_private.gym_panel2_series(v.venue, '30min'),
                       '60min', api_private.gym_panel2_series(v.venue, '60min')))
                   FROM v), '{}'::jsonb),
        -- panel 1's typical bars and day count are the same historical rollup
        -- as the heatmap, so they ride along here and stay off the five-minute
        -- path entirely.
        'panel1History', coalesce((SELECT jsonb_object_agg(v.venue,
                              api_private.gym_panel1_series(v.venue, true, false))
                          FROM v), '{}'::jsonb)
    );
$$;

CREATE OR REPLACE FUNCTION api_private.refresh_gym_heatmap_cache()
RETURNS timestamptz
LANGUAGE plpgsql
VOLATILE
SET search_path = ''
AS $$
DECLARE
    built jsonb := api_private.gym_heatmap_body();
    at_   timestamptz := now();
BEGIN
    INSERT INTO api_private.gym_heatmap_cache AS h (only_row, built_at, payload)
    VALUES (true, at_, built)
    ON CONFLICT (only_row) DO UPDATE
       SET built_at = excluded.built_at, payload = excluded.payload;
    RETURN at_;
END $$;

CREATE OR REPLACE FUNCTION public.gym_heatmap()
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    body jsonb;
    age  interval;
BEGIN
    PERFORM api_private.rate_limit_check('gym_heatmap');

    SELECT h.payload, now() - h.built_at INTO body, age
      FROM api_private.gym_heatmap_cache h WHERE h.only_row;

    -- An empty payload counts as stale however recently it was built. A
    -- database that has just been initialised caches an empty venue list before
    -- a single reading exists -- true of a fresh docker-compose stack, and of
    -- the restore drill, both of which run this file against an empty table --
    -- and without this it would then serve that emptiness for an hour.
    IF body IS NULL OR age > interval '1 hour' OR body -> 'venues' = '[]'::jsonb THEN
        -- One rebuild at a time. Losing the race is not an error: a slightly
        -- stale multi-month average is the right thing to serve, and it turns a
        -- cold-cache stampede from N rebuilds into one.
        IF pg_try_advisory_xact_lock(hashtext('api_private.gym_heatmap_cache')) THEN
            PERFORM api_private.refresh_gym_heatmap_cache();
            SELECT h.payload INTO body FROM api_private.gym_heatmap_cache h WHERE h.only_row;
        ELSIF body IS NULL THEN
            -- Truly cold and someone else is already building it. Rare enough
            -- to be worth paying for rather than failing.
            body := api_private.gym_heatmap_body();
        END IF;
    END IF;

    -- Freshness is never cached, even though the page ignores it here by
    -- design (only gym_live may set the staleness signal). Serving a stale
    -- servedAt would make this function lie to anything that did read it.
    RETURN body || jsonb_build_object(
        'dataAsOf', (SELECT max(o.fetched_at) FROM public.occupancy o),
        'servedAt', now(),
        'freshnessExpected', api_private.gym_is_open());
END $$;

CREATE OR REPLACE FUNCTION public.gym_live(p_days integer DEFAULT 7)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    t_to   timestamptz := now();
    t_from timestamptz;
BEGIN
    PERFORM api_private.rate_limit_check('gym_live');

    -- Clamped: the function is callable by anyone holding the public
    -- publishable key, and the rate limit bounds how often it can be called
    -- but not how much history one call may ask for.
    t_from := t_to - (least(greatest(coalesce(p_days, 7), 1), 31) || ' days')::interval;

    RETURN (
        WITH v AS (
            SELECT DISTINCT o.venue FROM public.occupancy o ORDER BY o.venue
        )
        SELECT jsonb_build_object(
            'dataAsOf', (SELECT max(o.fetched_at) FROM public.occupancy o),
            'servedAt', now(),
            'freshnessExpected', api_private.gym_is_open(),
            'panel1Now', coalesce((SELECT jsonb_object_agg(v.venue,
                              api_private.gym_panel1_series(v.venue, false, true))
                          FROM v), '{}'::jsonb),
            'panel3', coalesce((SELECT jsonb_object_agg(v.venue,
                          api_private.gym_panel3_series(v.venue, t_from, t_to))
                      FROM v), '{}'::jsonb),
            'panel4', api_private.gym_panel4_series()
        )
    );
END $$;

-- ---------------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------------
-- PostgreSQL grants EXECUTE to PUBLIC on every new function, and Supabase's
-- default privileges may additionally grant anon/authenticated explicitly --
-- so revoking PUBLIC alone would not prove anything. Revoke both, then grant
-- back deliberately.
--
-- Guarded on role existence throughout: apply_migrations.sh runs this file
-- against a bare postgres:17 container during the weekly restore drill, where
-- none of these roles exist.

DO $$
DECLARE
    fn text;
    role_name text;
    api_fns text[] := ARRAY[
        'api_private.gym_is_open(timestamptz)',
        'api_private.gym_panel1_typical_rows(text)',
        'api_private.gym_panel1_now_rows(text)',
        'api_private.gym_panel1_meta_rows(text)',
        'api_private.gym_panel2_rows(text,text)',
        'api_private.gym_panel3_rows(text,timestamptz,timestamptz)',
        'api_private.gym_panel4_rows()',
        'api_private.series_field(text,text,jsonb)',
        'api_private.gym_panel1_series(text,boolean,boolean)',
        'api_private.gym_panel2_series(text,text)',
        'api_private.gym_panel3_series(text,timestamptz,timestamptz)',
        'api_private.gym_panel4_series()'
    ];
    -- Cost-control internals. Revoked like everything else, but deliberately
    -- not granted to gym_reader: Grafana has no business refreshing a cache or
    -- charging itself a rate limit, and the wrappers reach them as owner.
    internal_fns text[] := ARRAY[
        'api_private.rate_limit_check(text)',
        'api_private.gym_heatmap_body()',
        'api_private.refresh_gym_heatmap_cache()'
    ];
    -- Unreachable already -- anon has no USAGE on api_private -- but a schema
    -- grant is one line away from being someone's quick fix, and these hold a
    -- rate limiter's state.
    internal_tables text[] := ARRAY[
        'api_private.rate_limit_policy',
        'api_private.rate_limit_client',
        'api_private.rate_limit_global',
        'api_private.gym_heatmap_cache'
    ];
    public_fns text[] := ARRAY[
        'public.gym_heatmap()',
        'public.gym_live(integer)'
    ];
BEGIN
    FOREACH fn IN ARRAY internal_tables LOOP
        EXECUTE format('REVOKE ALL ON TABLE %s FROM PUBLIC', fn);
        FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                EXECUTE format('REVOKE ALL ON TABLE %s FROM %I', fn, role_name);
            END IF;
        END LOOP;
    END LOOP;

    FOREACH fn IN ARRAY api_fns || internal_fns || public_fns LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', fn);
        FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I', fn, role_name);
            END IF;
        END LOOP;
    END LOOP;

    -- The browser gets exactly two entry points and nothing else.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        FOREACH fn IN ARRAY public_fns LOOP
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO anon', fn);
        END LOOP;
    END IF;

    -- Grafana's Supabase datasource connects as gym_reader and now selects
    -- from the relational primitives, so it needs to reach them. 003 grants
    -- SELECT on tables and views only -- without this the dashboard breaks the
    -- moment it is pointed at Supabase.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gym_reader') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA api_private TO gym_reader';
        FOREACH fn IN ARRAY api_fns LOOP
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO gym_reader', fn);
        END LOOP;
        FOREACH fn IN ARRAY public_fns LOOP
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO gym_reader', fn);
        END LOOP;
    END IF;
END $$;

-- Build the cache now rather than making the first visitor pay for it. Safe on
-- an empty database -- it stores an empty venue list, which gym_heatmap treats
-- as stale and rebuilds on demand.
SELECT api_private.refresh_gym_heatmap_cache();
