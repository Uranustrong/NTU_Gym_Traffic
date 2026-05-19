-- Bootstrap schema for the `occupancy` table.
-- Auto-applied by the postgres docker-entrypoint-initdb.d hook on the
-- VERY FIRST container start (when the data dir is empty). Subsequent
-- restarts skip this file.
--
-- Must stay in sync with `sync_to_postgres.py:SCHEMA_SQL`. Adding a
-- column means editing both, plus `OCCUPANCY_COLUMNS`, the `COPY`
-- column list in sync_to_postgres.py, and the `read_sqlite_rows`
-- SELECT clause.

CREATE TABLE IF NOT EXISTS occupancy (
    id BIGSERIAL PRIMARY KEY,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMP,
    venue TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    comfortable_count INTEGER,
    capacity_count INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_occupancy_fetched_at_venue
ON occupancy (fetched_at, venue);

CREATE TABLE IF NOT EXISTS occupancy_import (
    fetched_at TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMP,
    venue TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    comfortable_count INTEGER,
    capacity_count INTEGER
);
