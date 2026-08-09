"""Integration tests for sql/grafana_weekly_views.sql.

DESTRUCTIVE: creates and drops a schema. Guarded by dbtest_support, which
requires TEST_POSTGRES_URL plus ALLOW_DESTRUCTIVE_DB_TESTS=1 and ignores the
general-purpose POSTGRES_URL.

    TEST_POSTGRES_URL=postgresql://songhejun@127.0.0.1:5433/gym_fetch \\
    ALLOW_DESTRUCTIVE_DB_TESTS=1 \\
        python3 -m unittest test_grafana_weekly_views

The old `docker exec gym-postgres` fallback is gone: it reached a database
without going through any of these checks, and with the container running you
can point TEST_POSTGRES_URL at 127.0.0.1:5433 instead.
"""
import textwrap
import unittest
from pathlib import Path

from dbtest_support import destructive_tests_blocked, run_psql, scoped_name


VIEW_SQL = Path(__file__).with_name("sql").joinpath("grafana_weekly_views.sql")

# Per-process name, so a leftover from an interrupted run cannot collide with
# anything the database depends on.
TEST_SCHEMA = scoped_name("grafana_weekly_test")
BLOCKED = destructive_tests_blocked()


@unittest.skipIf(BLOCKED, BLOCKED or "")
class GrafanaWeeklyViewsTests(unittest.TestCase):
    def run_fixture_query(self, query):
        script = textwrap.dedent(
            f"""
            DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE;
            CREATE SCHEMA {TEST_SCHEMA};
            SET search_path TO {TEST_SCHEMA};

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

            DROP SCHEMA {TEST_SCHEMA} CASCADE;
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
