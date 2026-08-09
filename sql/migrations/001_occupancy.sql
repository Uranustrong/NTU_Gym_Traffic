-- 001: the occupancy table.
--
-- This is the canonical schema definition. sync_to_postgres.py deliberately
-- does NOT create tables any more: keeping DDL out of the sync path is what
-- lets the collector run as a SELECT+INSERT-only role (see 003).
--
-- Adding a column means editing this file, OCCUPANCY_COLUMNS in
-- sync_to_postgres.py (which drives the COPY list, the INSERT list and the
-- read_sqlite_rows SELECT), and the SQLite side in fetch_counts.py:connect().

CREATE TABLE IF NOT EXISTS occupancy (
    id BIGSERIAL PRIMARY KEY,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMP,
    venue TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    comfortable_count INTEGER,
    capacity_count INTEGER
);

-- Drives the ON CONFLICT clause in sync_to_postgres.py. Being a TIMESTAMPTZ
-- index it compares instants, not text, so rows written with different UTC
-- offsets still collide correctly.
CREATE UNIQUE INDEX IF NOT EXISTS ux_occupancy_fetched_at_venue
ON occupancy (fetched_at, venue);

-- Retire the shared staging table. It used to be truncated, COPYed into and
-- read back across three separate connections; two overlapping runs could
-- truncate each other's staged rows, losing a reading while still reporting
-- success. Staging is now a session-local TEMP TABLE ... ON COMMIT DROP.
--
-- Do NOT drop it blindly. That same three-connection design could also die
-- between the COPY and the UPSERT, in which case the staging table is holding
-- the only copy of a reading that never reached `occupancy`. Refuse to
-- continue if anything in there is unaccounted for.
DO $$
DECLARE
    stranded int;
BEGIN
    IF to_regclass('public.occupancy_import') IS NULL THEN
        RETURN;
    END IF;

    SELECT count(*) INTO stranded
    FROM occupancy_import i
    WHERE NOT EXISTS (
        SELECT 1 FROM occupancy o
        WHERE o.fetched_at = i.fetched_at
          AND o.venue = i.venue
    );

    IF stranded > 0 THEN
        RAISE EXCEPTION
            'occupancy_import holds % row(s) missing from occupancy; these may '
            'be the only copy. Rescue them first with: INSERT INTO occupancy '
            '(fetched_at, source_updated_at, venue, current_count, '
            'comfortable_count, capacity_count) SELECT fetched_at, '
            'source_updated_at, venue, current_count, comfortable_count, '
            'capacity_count FROM occupancy_import ON CONFLICT (fetched_at, '
            'venue) DO NOTHING;  -- then re-run this migration.', stranded;
    END IF;

    DROP TABLE occupancy_import;
    RAISE NOTICE 'dropped occupancy_import (every row was already in occupancy)';
END $$;
