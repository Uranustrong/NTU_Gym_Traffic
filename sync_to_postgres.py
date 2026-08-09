#!/usr/bin/env python3
"""Sync gym_counts.sqlite3 occupancy rows into PostgreSQL via the psql CLI.

Everything happens in ONE psql session and ONE transaction. That is load
bearing, not stylistic:

  * Staging is a `TEMP TABLE ... ON COMMIT DROP`, which is session-local, so
    two concurrent syncs cannot truncate each other's staged rows. The previous
    design used a shared `public.occupancy_import` across three separate psql
    processes; interleaved runs silently dropped a reading and still printed
    success.
  * The insert is verified inside the same transaction, against the exact
    `(fetched_at, venue)` keys staged by this run. A freshness check ("is there
    anything recent in the table?") cannot tell a successful write apart from a
    previous run's row.
  * That verification also makes retries safe. If a commit lands but the
    response is lost, re-running stages the same rows, hits the conflict clause,
    inserts nothing, and the verification join still finds them -- success,
    not a phantom failure.
  * Nothing here needs DDL rights on the `public` schema, so the runtime role
    can be restricted to SELECT + INSERT on `occupancy`. (`CREATE TEMP TABLE`
    is still DDL, but it only needs `TEMPORARY` on the database.)

Schema creation is deliberately NOT done here -- it lives in sql/migrations/
and is applied by tools/apply_migrations.sh with an owner-level credential.

Credentials: pass a password-free URL and let libpq read PGPASSWORD from the
environment. subprocess inherits os.environ, so nothing needs to be threaded
through. This keeps the password out of argv (visible in `ps`) and sidesteps
percent-encoding it into a URL.
"""
import argparse
import csv
import io
import re
import sqlite3
import subprocess
import sys
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

# Columns the upsert refreshes when --on-conflict update is requested. The key
# columns are excluded by definition.
UPDATABLE_COLUMNS = tuple(
    column for column in OCCUPANCY_COLUMNS if column not in ("fetched_at", "venue")
)

STAGING_DDL = """
CREATE TEMP TABLE occupancy_import (
    fetched_at TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMP,
    venue TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    comfortable_count INTEGER,
    capacity_count INTEGER
) ON COMMIT DROP;
"""

# Fails the transaction unless every staged row is present in `occupancy` with
# matching values. IS NOT DISTINCT FROM so NULL == NULL (recovered rows carry a
# NULL source_updated_at). Under --on-conflict nothing a mismatch means the row
# already existed with different values; that is a real conflict worth stopping
# on, and --on-conflict update is the deliberate way to resolve it.
VERIFY_SQL = """
DO $$
DECLARE
    missing int;
BEGIN
    SELECT count(*) INTO missing
    FROM occupancy_import i
    LEFT JOIN occupancy o
           ON o.fetched_at = i.fetched_at
          AND o.venue = i.venue
          AND o.current_count IS NOT DISTINCT FROM i.current_count
          AND o.source_updated_at IS NOT DISTINCT FROM i.source_updated_at
          AND o.comfortable_count IS NOT DISTINCT FROM i.comfortable_count
          AND o.capacity_count IS NOT DISTINCT FROM i.capacity_count
    WHERE o.fetched_at IS NULL;

    IF missing > 0 THEN
        RAISE EXCEPTION
            'sync verification failed: % of % staged row(s) missing or mismatched',
            missing, (SELECT count(*) FROM occupancy_import);
    END IF;
END $$;
"""


def read_sqlite_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            f"""
            SELECT {', '.join(OCCUPANCY_COLUMNS)}
            FROM occupancy
            ORDER BY fetched_at, venue
            """
        )
        return list(cursor)
    finally:
        conn.close()


def rows_to_csv(rows):
    """Render rows as COPY payload.

    lineterminator is "\\n", not the csv module default "\\r\\n". The payload is
    embedded in a script that psql splits into lines itself before handing them
    to COPY, so a trailing CR would survive into the last column and break the
    integer parse.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    return buffer.getvalue()


def conflict_clause(on_conflict):
    if on_conflict == "nothing":
        return "ON CONFLICT (fetched_at, venue) DO NOTHING"
    if on_conflict == "update":
        assignments = ",\n    ".join(
            f"{column} = EXCLUDED.{column}" for column in UPDATABLE_COLUMNS
        )
        return f"ON CONFLICT (fetched_at, venue) DO UPDATE SET\n    {assignments}"
    raise ValueError(f"unknown conflict strategy: {on_conflict!r}")


def build_sync_script(rows, on_conflict="nothing"):
    """Assemble the single script fed to `psql -f -`.

    Shaped like pg_dump output: the COPY payload sits inline, terminated by a
    lone `\\.` on its own line.
    """
    columns = ", ".join(OCCUPANCY_COLUMNS)
    return (
        "BEGIN;\n"
        f"{STAGING_DDL}"
        f"COPY occupancy_import ({columns}) FROM STDIN WITH (FORMAT csv);\n"
        f"{rows_to_csv(rows)}"
        "\\.\n"
        f"INSERT INTO occupancy ({columns})\n"
        f"SELECT {columns} FROM occupancy_import\n"
        f"{conflict_clause(on_conflict)};\n"
        f"{VERIFY_SQL}"
        "COMMIT;\n"
    )


def parse_written_count(psql_stdout):
    """Read the row count out of psql's `INSERT 0 N` command tag.

    This is what Postgres actually did, unlike the old code which reported how
    many rows were read out of SQLite regardless of the outcome. Under
    --on-conflict nothing the tag counts inserts only (skipped conflicts do not
    appear); under update it counts inserts and updates together -- hence
    "written" rather than "inserted".
    """
    matches = re.findall(r"^INSERT 0 (\d+)$", psql_stdout, re.MULTILINE)
    if not matches:
        # The INSERT always runs when there are rows, so a missing tag means
        # the output was not what we think it was -- silently reporting 0
        # would look exactly like a legitimate all-conflicts retry.
        raise RuntimeError(
            "no INSERT command tag in psql output; cannot confirm what was "
            f"written. Output was: {psql_stdout!r}")
    return int(matches[-1])


def run_psql_script(script, postgres_url, psql_path=DEFAULT_PSQL):
    completed = subprocess.run(
        # -X so a local ~/.psqlrc cannot suppress the command tags that
        # parse_written_count() reads, nor otherwise reshape the output.
        [psql_path, postgres_url, "-X", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def sync_rows(rows, postgres_url, psql_path=DEFAULT_PSQL, on_conflict="nothing"):
    """Stage, upsert and verify `rows`. Returns the number Postgres wrote."""
    if not rows:
        return 0
    script = build_sync_script(rows, on_conflict)
    return parse_written_count(run_psql_script(script, postgres_url, psql_path))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync gym_counts.sqlite3 occupancy rows into PostgreSQL."
    )
    parser.add_argument(
        "--sqlite-db",
        default=DEFAULT_SQLITE_DB,
        help=f"SQLite database path. Default: {DEFAULT_SQLITE_DB}",
    )
    parser.add_argument(
        "--postgres-url",
        required=True,
        help="PostgreSQL connection URL. Omit the password and set PGPASSWORD instead.",
    )
    parser.add_argument(
        "--psql",
        default=DEFAULT_PSQL,
        help=f"psql executable path. Default: {DEFAULT_PSQL}",
    )
    parser.add_argument(
        "--on-conflict",
        choices=("nothing", "update"),
        default="nothing",
        help="What to do when (fetched_at, venue) already exists. 'nothing' is "
             "right for routine collection and needs no UPDATE privilege. Use "
             "'update' with an owner credential for backfills and repairs.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.sqlite_db)
    rows = read_sqlite_rows(db_path)

    try:
        written = sync_rows(rows, args.postgres_url, args.psql, args.on_conflict)
    except subprocess.CalledProcessError as error:
        # psql writes the RAISE EXCEPTION text to stderr; surfacing it is the
        # difference between a diagnosable failure and an opaque exit code.
        sys.stderr.write(error.stderr or "")
        print(f"sync failed: psql exited {error.returncode}", file=sys.stderr)
        return 1

    print(f"read {len(rows)} rows from {db_path}; wrote {written} to PostgreSQL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
