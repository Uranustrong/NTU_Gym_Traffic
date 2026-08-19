-- ---------------------------------------------------------------------------
-- Closures -- when a venue was shut, so history can leave it out
-- ---------------------------------------------------------------------------
-- This file is named for the Grafana views it once held exclusively; it is now
-- also the bottom of the public API's stack. The closures table lives here
-- rather than in its own migration for one reason: weekly_occupancy_slots
-- depends on it, that view is 002, and a 001b_closures.sql would make
-- correctness depend on the shell's locale collation when apply_migrations.sh
-- expands `"$MIGRATIONS_DIR"/*.sql` -- which differs between a Mac and the
-- ubuntu runner that performs the weekly restore drill. Same file, no ordering
-- question.
--
-- Nothing here is schema-qualified, deliberately: test_grafana_weekly_views.py
-- applies this whole file into a throwaway schema via search_path, and a
-- hardcoded `public.` would write into the real one.

CREATE TABLE IF NOT EXISTS closures (
    venue     TEXT        NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    -- Half-open [starts_at, ends_at). NOT NULL: an open-ended closure sounds
    -- useful until you try to render it, and every closure this project has
    -- seen came with an end date. Add it when one does not.
    ends_at   TIMESTAMPTZ NOT NULL,
    -- Printed on the public page as-is, so English.
    reason    TEXT        NOT NULL,
    -- Where the claim came from. The banner renders it as a "(notice)" link,
    -- which is the only way a visitor can check the claim rather than trust it.
    source    TEXT,
    PRIMARY KEY (venue, starts_at),
    CONSTRAINT closures_range_ok CHECK (ends_at > starts_at)
);

-- Protections go on here, at creation, not in 004 where the rest of the
-- security story lives. apply_migrations.sh commits each file in its own
-- transaction, so 002 landing while 003 or 004 fails would otherwise leave
-- these objects in `public` -- which is to say on Supabase's Data API, since
-- its default privileges grant new objects in that schema to anon -- with no
-- RLS and no rate limiting, and leave them that way until someone noticed.
-- 004 repeats all of this; both are idempotent, and the duplication is the
-- point.
ALTER TABLE closures ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON closures FROM %I', role_name);
        END IF;
    END LOOP;
END $$;

-- DO UPDATE, not DO NOTHING -- the opposite of 005's rate_limit_policy seed,
-- and for the opposite reason. A rate limit is a tunable whose hand-edited
-- value is the correct one; a closure is a fact, and git should hold the only
-- copy. This also settles the backup question: backup.yml dumps only
-- public.occupancy, so closures is not in the dump -- but it is in version
-- control, and the restore drill rebuilds it from this file.
--
-- Consequence, stated rather than hidden: a hand-written UPDATE to these dates
-- is not in git, not in the backup, and will be overwritten the next time
-- apply_migrations.sh runs. Extending the renovation means editing this seed.
INSERT INTO closures (venue, starts_at, ends_at, reason, source) VALUES
    ('健身中心',
     TIMESTAMPTZ '2026-08-17 00:00:00+08',
     -- The announcement says "until 20 September", so the half-open range ends
     -- when the 21st begins.
     TIMESTAMPTZ '2026-09-21 00:00:00+08',
     'Renovation',
     'https://rent.pe.ntu.edu.tw/news/?K=157')
ON CONFLICT (venue, starts_at) DO UPDATE
    SET ends_at = EXCLUDED.ends_at,
        reason  = EXCLUDED.reason,
        source  = EXCLUDED.source;
-- One hazard this does NOT cover, and DO NOTHING would not either: the
-- conflict target is (venue, starts_at), so editing *those* fields in this
-- seed inserts a second row and leaves the old closure in place, silently
-- filtering a period nobody meant to filter. Changing a start date or a venue
-- therefore needs the old row named explicitly first:
--
--   DELETE FROM closures WHERE venue = '健身中心'
--                          AND starts_at = TIMESTAMPTZ '2026-08-17 00:00:00+08';
--
-- Changing only ends_at or reason is safe -- that is the common case (a
-- renovation slipping) and what DO UPDATE is here for.

-- The single definition of "was this venue closed at this instant". Two
-- consumers read it with different shapes -- occupancy_excluding_closures asks
-- per reading timestamp, api_private.active_closure_rows() asks about an
-- instant -- but neither restates the rule.
--
-- Why the monthly rule is generated rather than stored: a (starts_at, ends_at)
-- table cannot express a recurrence, and materialising the next few years of
-- fourth Mondays as rows creates a trap where forgetting to extend them fails
-- silently. generate_series derives its range from the data itself plus now(),
-- so there is nothing to maintain and nothing to expire.
CREATE OR REPLACE VIEW closure_periods
WITH (security_invoker = true) AS
WITH months AS (
    SELECT generate_series(
               date_trunc('month',
                   COALESCE((SELECT MIN(o.fetched_at) FROM occupancy o), now())
                   AT TIME ZONE 'Asia/Taipei'),
               date_trunc('month',
                   (now() AT TIME ZONE 'Asia/Taipei') + INTERVAL '1 month'),
               INTERVAL '1 month')::date AS month_start
),
fourth_mondays AS (
    -- Days to the month's first Monday, then three weeks on. ISODOW is 1 for
    -- Monday, so a month starting on Monday adds 0. The result always lands on
    -- the 22nd-28th, which is what "fourth Monday" means.
    SELECT month_start
           + ((8 - EXTRACT(ISODOW FROM month_start)::int) % 7)
           + 21 AS closed_on
    FROM months
),
-- Named, not a NULL wildcard. The notice says "健身中心及溫水泳池"; applying
-- the rule to whatever venue happens to appear in the data later would be a
-- claim about a venue nobody checked.
maintenance_venues (venue) AS (
    VALUES ('健身中心'::text), ('室內游泳池'::text)
)
SELECT venue, starts_at, ends_at, reason, source
FROM closures
UNION ALL
-- Posted in the site footer: "每個月第四個星期一為健身中心及溫水泳池休館日,
-- 17:00後開放". Confirmed against collected data: on 2026-05-25 the hourly peak
-- at 16:00 was 0 for 健身中心 and 4 for 室內游泳池, jumping to 121 and 29 at
-- 17:00. A normal Monday reads 76 and 20 in the same slots.
SELECT mv.venue,
       (fm.closed_on + TIME '00:00') AT TIME ZONE 'Asia/Taipei',
       (fm.closed_on + TIME '17:00') AT TIME ZONE 'Asia/Taipei',
       'Monthly maintenance'::text,
       'https://rent.pe.ntu.edu.tw/'::text
FROM fourth_mondays fm
CROSS JOIN maintenance_venues mv;

-- What every historical claim should read instead of `occupancy`.
--
-- `o.*` rather than a column list: CREATE OR REPLACE VIEW can append columns,
-- so re-running this file after occupancy gains one keeps the view in step
-- with no extra place to remember.
CREATE OR REPLACE VIEW occupancy_excluding_closures
WITH (security_invoker = true) AS
SELECT o.*
FROM occupancy o
WHERE NOT EXISTS (
    SELECT 1
    FROM closure_periods c
    WHERE c.venue = o.venue
      AND o.fetched_at >= c.starts_at
      AND o.fetched_at <  c.ends_at
);

-- Same reasoning as the table above. occupancy_excluding_closures matters most
-- of the three: as an owner-rights view it would read straight past
-- occupancy's RLS, which is exactly the bypass 004's header warns about.
-- security_invoker on the CREATE closes that; this closes the reachability.
DO $$
DECLARE
    obj text;
    role_name text;
BEGIN
    FOREACH obj IN ARRAY ARRAY['closure_periods', 'occupancy_excluding_closures'] LOOP
        FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                EXECUTE format('REVOKE ALL ON %I FROM %I', obj, role_name);
            END IF;
        END LOOP;
    END LOOP;
END $$;

CREATE OR REPLACE VIEW weekly_occupancy_slots AS
WITH localized AS (
    SELECT
        venue,
        fetched_at,
        fetched_at AT TIME ZONE 'Asia/Taipei' AS local_fetched_at,
        current_count,
        comfortable_count,
        capacity_count,
        CASE
            WHEN comfortable_count IS NOT NULL AND comfortable_count > 0
                THEN current_count::numeric / comfortable_count
            WHEN capacity_count IS NOT NULL AND capacity_count > 0
                THEN current_count::numeric / capacity_count
            ELSE NULL
        END AS crowding_ratio
    FROM occupancy_excluding_closures
),
slotted AS (
    SELECT
        venue,
        fetched_at,
        current_count,
        crowding_ratio,
        EXTRACT(ISODOW FROM local_fetched_at)::integer AS weekday_number,
        TO_CHAR(local_fetched_at, 'Dy') AS weekday_label,
        MAKE_TIME(
            EXTRACT(HOUR FROM local_fetched_at)::integer,
            (FLOOR(EXTRACT(MINUTE FROM local_fetched_at) / 30)::integer * 30),
            0
        ) AS slot_start_time,
        TO_CHAR(
            MAKE_TIME(
                EXTRACT(HOUR FROM local_fetched_at)::integer,
                (FLOOR(EXTRACT(MINUTE FROM local_fetched_at) / 30)::integer * 30),
                0
            ),
            'HH24:MI'
        ) AS slot_label
    FROM localized
),
open_slots AS (
    SELECT *
    FROM slotted
    WHERE
        (
            weekday_number BETWEEN 1 AND 5
            AND slot_start_time >= TIME '06:00'
            AND slot_start_time < TIME '22:00'
        )
        OR (
            weekday_number = 6
            AND slot_start_time >= TIME '09:00'
            AND slot_start_time < TIME '22:00'
        )
        OR (
            weekday_number = 7
            AND slot_start_time >= TIME '09:00'
            AND slot_start_time < TIME '18:00'
        )
)
SELECT
    venue,
    weekday_number,
    weekday_label,
    slot_start_time,
    slot_label,
    ROUND(AVG(current_count)::numeric, 2) AS avg_current_count,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY current_count)::numeric, 2) AS median_current_count,
    MIN(current_count) AS min_current_count,
    MAX(current_count) AS max_current_count,
    COUNT(*) AS sample_count,
    ROUND(AVG(crowding_ratio)::numeric, 4) AS avg_crowding_ratio
FROM open_slots
GROUP BY venue, weekday_number, weekday_label, slot_start_time, slot_label;

CREATE OR REPLACE VIEW current_vs_history AS
WITH latest AS (
    SELECT DISTINCT ON (venue)
        venue,
        fetched_at,
        current_count,
        comfortable_count,
        capacity_count
    FROM occupancy
    ORDER BY venue, fetched_at DESC
),
latest_slotted AS (
    SELECT
        venue,
        fetched_at AS latest_fetched_at,
        current_count,
        CASE
            WHEN comfortable_count IS NOT NULL AND comfortable_count > 0
                THEN current_count::numeric / comfortable_count
            WHEN capacity_count IS NOT NULL AND capacity_count > 0
                THEN current_count::numeric / capacity_count
            ELSE NULL
        END AS current_crowding_ratio,
        EXTRACT(ISODOW FROM fetched_at AT TIME ZONE 'Asia/Taipei')::integer AS weekday_number,
        MAKE_TIME(
            EXTRACT(HOUR FROM fetched_at AT TIME ZONE 'Asia/Taipei')::integer,
            (FLOOR(EXTRACT(MINUTE FROM fetched_at AT TIME ZONE 'Asia/Taipei') / 30)::integer * 30),
            0
        ) AS slot_start_time
    FROM latest
),
history AS (
    SELECT
        l.venue,
        l.latest_fetched_at,
        l.current_count,
        l.current_crowding_ratio,
        h.current_count AS historical_current_count
    FROM latest_slotted l
    -- The `latest` CTE above deliberately still reads `occupancy`: it is the
    -- live number, and the card must show what the sensor says even while the
    -- venue is shut. Only the thing it is compared against excludes closures.
    JOIN occupancy_excluding_closures h
        ON h.venue = l.venue
       AND h.fetched_at <> l.latest_fetched_at
       AND EXTRACT(ISODOW FROM h.fetched_at AT TIME ZONE 'Asia/Taipei')::integer = l.weekday_number
       AND MAKE_TIME(
            EXTRACT(HOUR FROM h.fetched_at AT TIME ZONE 'Asia/Taipei')::integer,
            (FLOOR(EXTRACT(MINUTE FROM h.fetched_at AT TIME ZONE 'Asia/Taipei') / 30)::integer * 30),
            0
       ) = l.slot_start_time
)
SELECT
    venue,
    latest_fetched_at,
    current_count,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY historical_current_count)::numeric, 2) AS historical_median_current_count,
    ROUND(AVG(historical_current_count)::numeric, 2) AS historical_avg_current_count,
    ROUND((current_count - PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY historical_current_count))::numeric, 2) AS difference_from_median,
    COUNT(*) AS sample_count,
    ROUND(current_crowding_ratio::numeric, 4) AS current_crowding_ratio
FROM history
GROUP BY venue, latest_fetched_at, current_count, current_crowding_ratio;
