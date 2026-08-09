"""Integration tests for sync_to_postgres against a real PostgreSQL.

The unit tests in test_sync_to_postgres.py mock subprocess, so they can prove
the *shape* of the generated script but not that PostgreSQL accepts it. The
parts most likely to break are exactly the parts mocks cannot reach:

  * the inline COPY payload and its lone `\\.` terminator, which psql splits
    into lines itself -- a CRLF here silently corrupts the last column
  * the PL/pgSQL verification block
  * whether a failed verification really rolls the whole transaction back
  * whether the script runs at all without DDL privileges on `public`

These tests are DESTRUCTIVE -- they drop schemas and roles -- so they refuse to
run unless explicitly armed, and they ignore the general-purpose POSTGRES_URL
entirely. See dbtest_support for the guardrails.

    TEST_POSTGRES_URL=postgresql://postgres@127.0.0.1:55432/postgres \\
    ALLOW_DESTRUCTIVE_DB_TESTS=1 \\
        python3 -m unittest test_sync_to_postgres_integration

Everything happens inside a per-process throwaway schema, selected via libpq's
`options=-csearch_path=...`, which is dropped afterwards.
"""
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path

import sync_to_postgres
from dbtest_support import (
    TEST_POSTGRES_URL, destructive_tests_blocked, run_psql, scoped_name,
    with_search_path,
)


TEST_SCHEMA = scoped_name("sync_it")
TEST_ROLE = scoped_name("gym_writer_it")
BLOCKED = destructive_tests_blocked()

OCCUPANCY_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA};
DROP TABLE IF EXISTS {TEST_SCHEMA}.occupancy;
CREATE TABLE {TEST_SCHEMA}.occupancy (
    id BIGSERIAL PRIMARY KEY,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMP,
    venue TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    comfortable_count INTEGER,
    capacity_count INTEGER
);
CREATE UNIQUE INDEX ux_sync_it_fetched_at_venue
    ON {TEST_SCHEMA}.occupancy (fetched_at, venue);
"""

ROWS = [
    ("2026-08-09T10:00:00+08:00", "2026-08-09 10:00", "健身中心", 22, 80, 161),
    # NULL source_updated_at, as produced by tools/recover_from_public_page.py
    ("2026-08-09T10:00:00+08:00", None, "室內游泳池", 9, 50, 130),
    ("2026-08-09T10:05:00+08:00", "2026-08-09 10:05", "健身中心", 25, 80, 161),
]


def psql(script, url=None):
    return run_psql(script, url)


@unittest.skipIf(BLOCKED, BLOCKED or "")
class SyncIntegrationTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        psql(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE;")

    def setUp(self):
        psql(OCCUPANCY_DDL)
        self.url = with_search_path(TEST_POSTGRES_URL, TEST_SCHEMA)
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = Path(temp_dir.name) / "gym_counts.sqlite3"
        self.write_sqlite(ROWS)

    def write_sqlite(self, rows):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE IF EXISTS occupancy")
        conn.execute(
            """
            CREATE TABLE occupancy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at TEXT NOT NULL,
                source_updated_at TEXT,
                venue TEXT NOT NULL,
                current_count INTEGER NOT NULL,
                comfortable_count INTEGER,
                capacity_count INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO occupancy (fetched_at, source_updated_at, venue, "
            "current_count, comfortable_count, capacity_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

    def sync(self, on_conflict="nothing", url=None):
        rows = sync_to_postgres.read_sqlite_rows(self.db_path)
        return sync_to_postgres.sync_rows(
            rows, url or self.url, "psql", on_conflict)

    def query(self, sql):
        return psql(f"SET search_path TO {TEST_SCHEMA}; {sql}")

    def test_rows_round_trip_with_nulls_and_non_ascii_intact(self):
        written = self.sync()

        self.assertEqual(written, 3)
        self.assertEqual(self.query("SELECT count(*) FROM occupancy;"), "3")
        self.assertEqual(
            self.query(
                "SELECT current_count FROM occupancy "
                "WHERE venue = '健身中心' "
                "AND fetched_at = '2026-08-09T10:00:00+08:00';"),
            "22",
        )
        # The recovered-row marker must survive the COPY round trip.
        self.assertEqual(
            self.query("SELECT count(*) FROM occupancy "
                       "WHERE source_updated_at IS NULL;"),
            "1",
        )

    def test_copy_payload_leaves_no_carriage_returns_in_the_last_column(self):
        # A CRLF-terminated payload would either fail the integer parse or
        # smuggle a CR into the trailing text column.
        self.sync()

        self.assertEqual(
            self.query(r"SELECT count(*) FROM occupancy WHERE venue LIKE E'%\r%';"),
            "0",
        )
        self.assertEqual(
            self.query("SELECT sum(capacity_count) FROM occupancy;"),
            str(161 + 130 + 161),
        )

    def test_rerunning_is_idempotent_and_writes_nothing(self):
        self.sync()

        written = self.sync()

        self.assertEqual(written, 0)
        self.assertEqual(self.query("SELECT count(*) FROM occupancy;"), "3")

    def test_staging_table_never_outlives_the_transaction(self):
        self.sync()

        self.assertEqual(
            self.query("SELECT count(*) FROM pg_tables "
                       "WHERE tablename = 'occupancy_import';"),
            "0",
        )

    def test_mismatched_row_fails_and_rolls_the_whole_transaction_back(self):
        self.sync()
        # Change one existing reading and add a brand-new one. Under DO NOTHING
        # the changed row cannot be written, so verification must fail -- and
        # the new row must vanish with it, or the sync was not atomic.
        mutated = list(ROWS) + [
            ("2026-08-09T10:10:00+08:00", "2026-08-09 10:10", "健身中心", 30, 80, 161)]
        mutated[0] = (*ROWS[0][:3], 999, *ROWS[0][4:])
        self.write_sqlite(mutated)

        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.sync()

        self.assertIn("sync verification failed", caught.exception.stderr)
        self.assertEqual(
            self.query("SELECT count(*) FROM occupancy "
                       "WHERE fetched_at = '2026-08-09T10:10:00+08:00';"),
            "0",
            "the new row survived a failed verification - transaction not atomic",
        )
        self.assertEqual(
            self.query("SELECT current_count FROM occupancy "
                       "WHERE venue = '健身中心' "
                       "AND fetched_at = '2026-08-09T10:00:00+08:00';"),
            "22",
            "DO NOTHING must never overwrite an existing reading",
        )

    def test_on_conflict_update_resolves_a_mismatch(self):
        self.sync()
        mutated = list(ROWS)
        mutated[0] = (*ROWS[0][:3], 999, *ROWS[0][4:])
        self.write_sqlite(mutated)

        self.sync(on_conflict="update")

        self.assertEqual(
            self.query("SELECT current_count FROM occupancy "
                       "WHERE venue = '健身中心' "
                       "AND fetched_at = '2026-08-09T10:00:00+08:00';"),
            "999",
        )

    def test_a_select_insert_only_role_can_complete_a_sync(self):
        # The whole point of TEMP staging: the collector credential needs no
        # DDL on public, no UPDATE, no DELETE, no TRUNCATE.
        database = psql("SELECT current_database();")
        # TEST_ROLE carries a per-process suffix, so this can never collide
        # with a role the database actually depends on.
        try:
            psql(
                f"""
                -- A plain DROP ROLE IF EXISTS is not enough: it errors out
                -- while the role still holds grants, so a leftover role from
                -- an interrupted run would masquerade as "this DB will not let
                -- me create roles" and skip the test.
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles
                               WHERE rolname = '{TEST_ROLE}') THEN
                        EXECUTE 'DROP OWNED BY {TEST_ROLE}';
                        EXECUTE 'DROP ROLE {TEST_ROLE}';
                    END IF;
                END $$;
                CREATE ROLE {TEST_ROLE} LOGIN;
                GRANT USAGE ON SCHEMA {TEST_SCHEMA} TO {TEST_ROLE};
                GRANT TEMPORARY ON DATABASE {database} TO {TEST_ROLE};
                GRANT SELECT, INSERT ON {TEST_SCHEMA}.occupancy TO {TEST_ROLE};
                GRANT USAGE, SELECT ON SEQUENCE {TEST_SCHEMA}.occupancy_id_seq
                    TO {TEST_ROLE};
                """
            )
        except subprocess.CalledProcessError as error:
            self.skipTest(f"cannot create a test role here: {error.stderr}")
        # DROP ROLE refuses while the role still holds grants, so shed those
        # first -- DROP OWNED BY covers privileges as well as owned objects.
        self.addCleanup(lambda: psql(
            f"DROP OWNED BY {TEST_ROLE}; DROP ROLE IF EXISTS {TEST_ROLE};"))

        parts = urllib.parse.urlsplit(
            with_search_path(TEST_POSTGRES_URL, TEST_SCHEMA))
        writer_url = urllib.parse.urlunsplit(
            parts._replace(netloc=f"{TEST_ROLE}@{parts.hostname}:{parts.port}"))

        self.assertEqual(self.sync(url=writer_url), 3)


if __name__ == "__main__":
    unittest.main()
