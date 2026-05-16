import sqlite3
import tempfile
import unittest
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

        self.assertEqual(csv_text, "2026-05-14T09:00:00+08:00,,游泳池,5,,60\r\n")

    @mock.patch("sync_to_postgres.subprocess.run")
    def test_sync_rows_creates_schema_copies_rows_and_upserts(self, run):
        sync_to_postgres.sync_rows(
            [("2026-05-14T09:00:00+08:00", "2026-05-14 08:59", "健身房", 12, 40, 80)],
            "postgresql://user:pass@localhost:5432/gym_fetch",
            "psql",
        )

        self.assertEqual(run.call_count, 3)
        self.assertIn("CREATE TABLE IF NOT EXISTS occupancy", run.call_args_list[0].args[0][-1])
        self.assertIn("COPY occupancy_import", run.call_args_list[1].args[0][-1])
        self.assertEqual(
            run.call_args_list[1].kwargs["input"],
            "2026-05-14T09:00:00+08:00,2026-05-14 08:59,健身房,12,40,80\r\n",
        )
        self.assertIn("ON CONFLICT (fetched_at, venue) DO UPDATE", run.call_args_list[2].args[0][-1])


if __name__ == "__main__":
    unittest.main()
