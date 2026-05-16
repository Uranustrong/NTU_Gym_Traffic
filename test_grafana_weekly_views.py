import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path


POSTGRES_URL = os.environ.get(
    "POSTGRES_URL",
    "postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch",
)
VIEW_SQL = Path(__file__).with_name("sql").joinpath("grafana_weekly_views.sql")


def run_psql(script):
    if shutil.which("psql"):
        command = ["psql", POSTGRES_URL, "-v", "ON_ERROR_STOP=1", "-qAt"]
    else:
        command = [
            "/opt/homebrew/bin/docker",
            "exec",
            "-i",
            "gym-postgres",
            "psql",
            "-U",
            "songhejun",
            "-d",
            "gym_fetch",
            "-v",
            "ON_ERROR_STOP=1",
            "-qAt",
        ]

    return subprocess.run(
        command,
        input=script,
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()


class GrafanaWeeklyViewsTests(unittest.TestCase):
    def run_fixture_query(self, query):
        script = textwrap.dedent(
            f"""
            DROP SCHEMA IF EXISTS grafana_weekly_test CASCADE;
            CREATE SCHEMA grafana_weekly_test;
            SET search_path TO grafana_weekly_test;

            CREATE TABLE occupancy (
                id BIGSERIAL PRIMARY KEY,
                fetched_at TIMESTAMPTZ NOT NULL,
                source_updated_at TIMESTAMP,
                venue TEXT NOT NULL,
                current_count INTEGER NOT NULL,
                comfortable_count INTEGER,
                capacity_count INTEGER
            );

            INSERT INTO occupancy (
                fetched_at,
                source_updated_at,
                venue,
                current_count,
                comfortable_count,
                capacity_count
            ) VALUES
                ('2026-05-04T08:05:00+08:00', NULL, '健身房', 10, 40, 80),
                ('2026-05-11T08:20:00+08:00', NULL, '健身房', 20, 40, 80),
                ('2026-05-18T08:35:00+08:00', NULL, '健身房', 30, 40, 80),
                ('2026-05-25T08:45:00+08:00', NULL, '健身房', 50, NULL, 100),
                ('2026-05-03T18:10:00+08:00', NULL, '健身房', 99, 40, 80),
                ('2026-05-10T09:05:00+08:00', NULL, '游泳池', 12, NULL, 60),
                ('2026-05-17T09:20:00+08:00', NULL, '游泳池', 18, NULL, 60);

            {VIEW_SQL.read_text(encoding="utf-8")}

            {query}

            DROP SCHEMA grafana_weekly_test CASCADE;
            """
        )
        return run_psql(script)

    def test_weekly_slots_bucket_30_minutes_and_open_hours(self):
        output = self.run_fixture_query(
            """
            SELECT venue, weekday_number, slot_label, avg_current_count, median_current_count, sample_count, avg_crowding_ratio
            FROM weekly_occupancy_slots
            WHERE venue = '健身房'
            ORDER BY weekday_number, slot_label;
            """
        )

        self.assertEqual(
            output.splitlines(),
            [
                "健身房|1|08:00|15.00|15.00|2|0.3750",
                "健身房|1|08:30|40.00|40.00|2|0.6250",
            ],
        )

    def test_current_vs_history_excludes_latest_row(self):
        output = self.run_fixture_query(
            """
            SELECT venue, current_count, historical_median_current_count, historical_avg_current_count, difference_from_median, sample_count
            FROM current_vs_history
            WHERE venue = '健身房';
            """
        )

        self.assertEqual(output, "健身房|50|30.00|30.00|20.00|1")


if __name__ == "__main__":
    unittest.main()
