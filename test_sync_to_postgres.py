import argparse
import io
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import sync_to_postgres


class SyncToPostgresTests(unittest.TestCase):
    def make_sqlite_db(self):
        temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(temp_dir.name) / "gym_counts.sqlite3"
        conn = sqlite3.connect(db_path)
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
            """
            INSERT INTO occupancy (
                fetched_at, source_updated_at, venue,
                current_count, comfortable_count, capacity_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-05-14T09:00:00+08:00", "2026-05-14 08:59", "健身房", 12, 40, 80),
                ("2026-05-14T09:00:00+08:00", None, "游泳池", 5, None, 60),
            ],
        )
        conn.commit()
        conn.close()
        self.addCleanup(temp_dir.cleanup)
        return db_path

    def test_read_sqlite_rows_returns_rows_in_import_order(self):
        db_path = self.make_sqlite_db()

        rows = sync_to_postgres.read_sqlite_rows(db_path)

        self.assertEqual(
            rows,
            [
                ("2026-05-14T09:00:00+08:00", "2026-05-14 08:59", "健身房", 12, 40, 80),
                ("2026-05-14T09:00:00+08:00", None, "游泳池", 5, None, 60),
            ],
        )

    def test_rows_to_csv_uses_empty_fields_for_nulls(self):
        csv_text = sync_to_postgres.rows_to_csv(
            [
                ("2026-05-14T09:00:00+08:00", None, "游泳池", 5, None, 60),
            ]
        )

        self.assertEqual(csv_text, "2026-05-14T09:00:00+08:00,,游泳池,5,,60\n")

    def test_rows_to_csv_never_emits_carriage_returns(self):
        # The payload is embedded in a script that psql splits into lines
        # itself; a trailing CR would ride into the last column and break the
        # integer parse on the far side.
        csv_text = sync_to_postgres.rows_to_csv(
            [
                ("2026-05-14T09:00:00+08:00", "2026-05-14 08:59", "健身房", 12, 40, 80),
                ("2026-05-14T09:05:00+08:00", "2026-05-14 09:04", "游泳池", 5, 50, 60),
            ]
        )

        self.assertNotIn("\r", csv_text)
        self.assertTrue(csv_text.endswith("60\n"))


class SyncScriptTests(unittest.TestCase):
    ROWS = [("2026-05-14T09:00:00+08:00", "2026-05-14 08:59", "健身房", 12, 40, 80)]

    def test_script_is_one_transaction_around_temp_staging(self):
        script = sync_to_postgres.build_sync_script(self.ROWS)

        for fragment in (
            "BEGIN;",
            "CREATE TEMP TABLE occupancy_import",
            "ON COMMIT DROP",
            "COPY occupancy_import",
            "\\.",
            "INSERT INTO occupancy",
            "RAISE EXCEPTION",
            "COMMIT;",
        ):
            self.assertIn(fragment, script)

        positions = [
            script.index("BEGIN;"),
            script.index("CREATE TEMP TABLE occupancy_import"),
            script.index("COPY occupancy_import"),
            script.index("\\.\n"),
            script.index("INSERT INTO occupancy"),
            script.index("DO $$"),
            script.index("COMMIT;"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_script_never_creates_the_shared_staging_table(self):
        # A non-TEMP occupancy_import is what let concurrent runs truncate each
        # other's staged rows, and it would require DDL rights the collector
        # role deliberately does not have.
        script = sync_to_postgres.build_sync_script(self.ROWS)

        self.assertNotIn("CREATE TABLE IF NOT EXISTS occupancy_import", script)
        self.assertNotIn("TRUNCATE", script)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS occupancy ", script)

    def test_copy_payload_sits_between_the_copy_statement_and_the_terminator(self):
        script = sync_to_postgres.build_sync_script(self.ROWS)

        body = script[script.index("FROM STDIN WITH (FORMAT csv);\n"):script.index("\\.\n")]
        self.assertIn("2026-05-14T09:00:00+08:00,2026-05-14 08:59,健身房,12,40,80", body)

    def test_default_conflict_strategy_inserts_only(self):
        script = sync_to_postgres.build_sync_script(self.ROWS)

        self.assertIn("ON CONFLICT (fetched_at, venue) DO NOTHING", script)
        self.assertNotIn("DO UPDATE", script)

    def test_update_strategy_refreshes_every_non_key_column(self):
        script = sync_to_postgres.build_sync_script(self.ROWS, on_conflict="update")

        self.assertIn("ON CONFLICT (fetched_at, venue) DO UPDATE SET", script)
        for column in ("source_updated_at", "current_count",
                       "comfortable_count", "capacity_count"):
            self.assertIn(f"{column} = EXCLUDED.{column}", script)
        # The key columns must never be reassigned.
        self.assertNotIn("fetched_at = EXCLUDED.fetched_at", script)
        self.assertNotIn("venue = EXCLUDED.venue", script)

    def test_unknown_conflict_strategy_is_rejected(self):
        with self.assertRaises(ValueError):
            sync_to_postgres.build_sync_script(self.ROWS, on_conflict="merge")


class SyncRowsTests(unittest.TestCase):
    ROWS = [("2026-05-14T09:00:00+08:00", "2026-05-14 08:59", "健身房", 12, 40, 80)]

    @mock.patch("sync_to_postgres.subprocess.run")
    def test_uses_a_single_psql_invocation(self, run):
        run.return_value = mock.Mock(stdout="INSERT 0 1\n")

        sync_to_postgres.sync_rows(
            self.ROWS, "postgresql://user@localhost:5432/gym_fetch", "psql")

        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2:], ["-f", "-"])
        self.assertIn("ON_ERROR_STOP=1", argv)
        # -X: a local ~/.psqlrc must not be able to suppress the command tags
        # the written-row count is parsed from.
        self.assertIn("-X", argv)
        self.assertIn("BEGIN;", run.call_args.kwargs["input"])

    @mock.patch("sync_to_postgres.subprocess.run")
    def test_missing_command_tag_raises_instead_of_reporting_zero(self, run):
        # Reporting 0 here would be indistinguishable from a legitimate
        # all-conflicts retry, turning a broken run into a silent success.
        run.return_value = mock.Mock(stdout="BEGIN\nCOPY 1\nDO\nCOMMIT\n")

        with self.assertRaises(RuntimeError):
            sync_to_postgres.sync_rows(
                self.ROWS, "postgresql://user@localhost:5432/gym_fetch", "psql")

    @mock.patch("sync_to_postgres.subprocess.run")
    def test_reports_rows_actually_written_not_rows_read(self, run):
        run.return_value = mock.Mock(stdout="BEGIN\nCOPY 2\nINSERT 0 2\nDO\nCOMMIT\n")

        inserted = sync_to_postgres.sync_rows(
            self.ROWS * 2, "postgresql://user@localhost:5432/gym_fetch", "psql")

        self.assertEqual(inserted, 2)

    @mock.patch("sync_to_postgres.subprocess.run")
    def test_reports_zero_when_every_row_hit_the_conflict_clause(self, run):
        # The retry-after-lost-response case: rows were already committed, so
        # nothing is inserted, yet the run is a success.
        run.return_value = mock.Mock(stdout="BEGIN\nCOPY 1\nINSERT 0 0\nDO\nCOMMIT\n")

        inserted = sync_to_postgres.sync_rows(
            self.ROWS, "postgresql://user@localhost:5432/gym_fetch", "psql")

        self.assertEqual(inserted, 0)

    @mock.patch("sync_to_postgres.subprocess.run")
    def test_no_rows_means_no_connection_at_all(self, run):
        inserted = sync_to_postgres.sync_rows(
            [], "postgresql://user@localhost:5432/gym_fetch", "psql")

        self.assertEqual(inserted, 0)
        run.assert_not_called()

    @mock.patch("sync_to_postgres.subprocess.run")
    def test_psql_failure_propagates(self, run):
        run.side_effect = subprocess.CalledProcessError(
            returncode=3, cmd=["psql"], stderr="ERROR:  sync verification failed\n")

        with self.assertRaises(subprocess.CalledProcessError):
            sync_to_postgres.sync_rows(
                self.ROWS, "postgresql://user@localhost:5432/gym_fetch", "psql")


class MainTests(unittest.TestCase):
    def run_main(self, db_path, run_mock):
        args = argparse.Namespace(
            sqlite_db=str(db_path),
            postgres_url="postgresql://user@localhost:5432/gym_fetch",
            psql="psql",
            on_conflict="nothing",
        )
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sync_to_postgres.parse_args", return_value=args), \
                mock.patch("sync_to_postgres.subprocess.run", run_mock), \
                redirect_stdout(out), redirect_stderr(err):
            code = sync_to_postgres.main()
        return code, out.getvalue(), err.getvalue()

    def test_reports_success_only_when_psql_succeeded(self):
        db_path = SyncToPostgresTests.make_sqlite_db(self)
        run = mock.Mock(return_value=mock.Mock(stdout="INSERT 0 2\n"))

        code, out, _ = self.run_main(db_path, run)

        self.assertEqual(code, 0)
        self.assertIn("wrote 2", out)

    def test_verification_failure_exits_nonzero_and_surfaces_the_reason(self):
        db_path = SyncToPostgresTests.make_sqlite_db(self)
        run = mock.Mock(side_effect=subprocess.CalledProcessError(
            returncode=3,
            cmd=["psql"],
            stderr="ERROR:  sync verification failed: 2 of 2 staged row(s) missing\n",
        ))

        code, out, err = self.run_main(db_path, run)

        self.assertEqual(code, 1)
        self.assertNotIn("wrote", out)
        self.assertIn("sync verification failed", err)


if __name__ == "__main__":
    unittest.main()
