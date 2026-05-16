#!/usr/bin/env python3
import argparse
import csv
import io
import sqlite3
import subprocess
from pathlib import Path


DEFAULT_SQLITE_DB = "gym_counts.sqlite3"
DEFAULT_PSQL = "psql"

OCCUPANCY_COLUMNS = (
    "fetched_at",
    "source_updated_at",
    "venue",
    "current_count",
    "comfortable_count",
    "capacity_count",
)

SCHEMA_SQL = """
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

TRUNCATE occupancy_import;
"""

COPY_SQL = """
COPY occupancy_import (
    fetched_at,
    source_updated_at,
    venue,
    current_count,
    comfortable_count,
    capacity_count
) FROM STDIN WITH (FORMAT csv);
"""

UPSERT_SQL = """
INSERT INTO occupancy (
    fetched_at,
    source_updated_at,
    venue,
    current_count,
    comfortable_count,
    capacity_count
)
SELECT
    fetched_at,
    source_updated_at,
    venue,
    current_count,
    comfortable_count,
    capacity_count
FROM occupancy_import
ON CONFLICT (fetched_at, venue) DO UPDATE SET
    source_updated_at = EXCLUDED.source_updated_at,
    current_count = EXCLUDED.current_count,
    comfortable_count = EXCLUDED.comfortable_count,
    capacity_count = EXCLUDED.capacity_count;

TRUNCATE occupancy_import;
"""


def read_sqlite_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT fetched_at, source_updated_at, venue,
                   current_count, comfortable_count, capacity_count
            FROM occupancy
            ORDER BY fetched_at, venue
            """
        )
        return list(cursor)
    finally:
        conn.close()


def rows_to_csv(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    return buffer.getvalue()


def run_psql(sql, postgres_url, psql_path, input_text=None):
    return subprocess.run(
        [psql_path, postgres_url, "-v", "ON_ERROR_STOP=1", "-c", sql],
        input=input_text,
        text=True,
        check=True,
    )


def sync_rows(rows, postgres_url, psql_path=DEFAULT_PSQL):
    run_psql(SCHEMA_SQL, postgres_url, psql_path)
    if rows:
        run_psql(COPY_SQL, postgres_url, psql_path, input_text=rows_to_csv(rows))
    run_psql(UPSERT_SQL, postgres_url, psql_path)
    return len(rows)


def parse_args():
    parser = argparse.ArgumentParser(description="Sync gym_counts.sqlite3 occupancy rows into PostgreSQL.")
    parser.add_argument("--sqlite-db", default=DEFAULT_SQLITE_DB, help=f"SQLite database path. Default: {DEFAULT_SQLITE_DB}")
    parser.add_argument(
        "--postgres-url",
        required=True,
        help="PostgreSQL connection URL, for example postgresql://user:pass@localhost:5432/gym_fetch",
    )
    parser.add_argument("--psql", default=DEFAULT_PSQL, help=f"psql executable path. Default: {DEFAULT_PSQL}")
    return parser.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.sqlite_db)
    rows = read_sqlite_rows(db_path)
    count = sync_rows(rows, args.postgres_url, args.psql)
    print(f"synced {count} rows from {db_path} to PostgreSQL")


if __name__ == "__main__":
    main()
