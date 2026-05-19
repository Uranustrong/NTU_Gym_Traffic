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
    FROM occupancy
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
    JOIN occupancy h
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
