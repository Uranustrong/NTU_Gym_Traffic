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
                ('2026-05-04T08:05:00+08:00', NULL, '健身中心', 10, 40, 80),
                ('2026-05-11T08:20:00+08:00', NULL, '健身中心', 20, 40, 80),
                ('2026-05-18T08:35:00+08:00', NULL, '健身中心', 30, 40, 80),
                -- 2026-05-25 is the fourth Monday of May. These two straddle
                -- the 17:00 boundary the monthly maintenance rule turns on.
                ('2026-05-25T08:45:00+08:00', NULL, '健身中心', 50, NULL, 100),
                ('2026-05-25T16:59:00+08:00', NULL, '健身中心', 777, 40, 80),
                ('2026-05-25T17:00:00+08:00', NULL, '健身中心', 888, 40, 80),
                ('2026-05-03T18:10:00+08:00', NULL, '健身中心', 99, 40, 80),
                ('2026-05-10T09:05:00+08:00', NULL, '室內游泳池', 12, NULL, 60),
                ('2026-05-17T09:20:00+08:00', NULL, '室內游泳池', 18, NULL, 60),
                -- The monthly rule names both venues, so this one goes too.
                ('2026-05-25T10:00:00+08:00', NULL, '室內游泳池', 555, NULL, 60),
                -- A venue the rule does NOT name. It must survive: this is the
                -- regression guard for the change from "NULL means every
                -- venue" to two named ones.
                ('2026-05-25T10:00:00+08:00', NULL, '壁球室', 7, NULL, 20),
                -- Same instant, two venues. A closure naming only one of them
                -- must leave the other alone -- without a same-instant pair the
                -- test passes even if the venue condition is dropped entirely.
                ('2026-05-19T09:00:00+08:00', NULL, '室內游泳池', 61, NULL, 60),
                ('2026-05-19T09:00:00+08:00', NULL, '健身中心', 62, 40, 80),
                -- current_vs_history: the latest 健身中心 row is 06-23
                -- 09:00. These give it three same-weekday, same-slot
                -- neighbours, so closing one visibly moves the median.
                --
                -- 06-23 rather than 05-26 -- also a Tuesday, so every weekly
                -- aggregate below is unchanged -- because it has to stay
                -- *later* than the 06-19 rows added under it. `latest` reads
                -- raw occupancy on purpose, so a 06-19 row would otherwise
                -- become the latest and drop 健身中心 out of the view.
                ('2026-05-05T09:00:00+08:00', NULL, '健身中心', 20, 40, 80),
                ('2026-05-12T09:00:00+08:00', NULL, '健身中心', 40, 40, 80),
                ('2026-06-23T09:00:00+08:00', NULL, '健身中心', 999, 40, 80),
                -- 2026-06-19 is 端午節, a Friday, and the day both venues
                -- reported 0 for every open-hour reading. 壁球室 rides along
                -- for the same reason it does on 05-25: the seed names two
                -- venues, and a wildcard would take this one too.
                ('2026-06-19T09:00:00+08:00', NULL, '健身中心', 321, 40, 80),
                ('2026-06-19T09:00:00+08:00', NULL, '室內游泳池', 322, NULL, 60),
                ('2026-06-19T09:00:00+08:00', NULL, '壁球室', 9, NULL, 20);

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
            WHERE venue = '健身中心'
            ORDER BY weekday_number, slot_label;
            """
        )

        self.assertEqual(
            output.splitlines(),
            [
                # 05-04's 10 and 05-11's 20, both comfortable 40.
                "健身中心|1|08:00|15.00|15.00|2|0.3750",
                # 05-25 08:45's 50 falls inside the fourth-Monday window, so
                # this bucket keeps only 05-18's 30 -- and its ratio is
                # 30/40 = 0.75 rather than the old two-row average, because the
                # excluded row was the one with capacity 100 instead of
                # comfortable 40.
                "健身中心|1|08:30|30.00|30.00|1|0.7500",
                # 17:00 is after the maintenance window closes. 888/40 = 22.2.
                "健身中心|1|17:00|888.00|888.00|1|22.2000",
                # Tuesdays 05-05, 05-12, 05-19 and 06-23: 20, 40, 62, 999.
                # Mean 280.25, median (40+62)/2 = 51, ratios averaging
                # (0.5 + 1.0 + 1.55 + 24.975) / 4 = 7.00625.
                "健身中心|2|09:00|280.25|51.00|4|7.0063",
            ],
        )

    def test_fourth_monday_is_excluded_up_to_17_00(self):
        """Fourth-Monday readings before 17:00 do not count as history."""
        output = self.run_fixture_query(
            """
            SELECT to_char(fetched_at AT TIME ZONE 'Asia/Taipei', 'HH24:MI'), current_count
            FROM occupancy_excluding_closures
            WHERE venue = '健身中心'
              AND (fetched_at AT TIME ZONE 'Asia/Taipei')::date = DATE '2026-05-25'
            ORDER BY fetched_at;
            """
        )

        # 08:45's 50 and 16:59's 777 are gone; 17:00's 888 stays.
        self.assertEqual(output.splitlines(), ["17:00|888"])

    def test_fourth_monday_applies_to_both_named_venues_only(self):
        """The rule names 健身中心 and 室內游泳池. Other venues are untouched."""
        output = self.run_fixture_query(
            """
            SELECT venue, count(*)
            FROM occupancy_excluding_closures
            WHERE (fetched_at AT TIME ZONE 'Asia/Taipei')::date = DATE '2026-05-25'
              AND (fetched_at AT TIME ZONE 'Asia/Taipei')::time < TIME '17:00'
            GROUP BY venue
            ORDER BY venue;
            """
        )

        self.assertEqual(output.splitlines(), ["壁球室|1"])

    def test_a_closure_row_only_hides_the_venue_it_names(self):
        """Two venues share the instant 2026-05-19 09:00; the closure names one.

        Without the same-instant pair this test would pass even if the venue
        condition were ignored altogether.
        """
        output = self.run_fixture_query(
            """
            INSERT INTO closures (venue, starts_at, ends_at, reason)
            VALUES ('室內游泳池',
                    TIMESTAMPTZ '2026-05-19T00:00:00+08:00',
                    TIMESTAMPTZ '2026-05-20T00:00:00+08:00',
                    'Test closure');

            SELECT venue, current_count
            FROM occupancy_excluding_closures
            WHERE fetched_at = TIMESTAMPTZ '2026-05-19T09:00:00+08:00'
            ORDER BY venue;
            """
        )

        self.assertEqual(output.splitlines(), ["健身中心|62"])

    def test_current_vs_history_compares_against_open_days_only(self):
        """The latest reading is real; what it is compared against is filtered.

        Latest is 06-23 09:00's 999. Same ISODOW=2, same 09:00 slot, there are
        three historical neighbours: 05-05's 20, 05-12's 40 and 05-19's 62.
        Closing 05-19 moves the median from 40 to 30 and the sample count from
        3 to 2. Before the change this returns 999|40.00|3.
        """
        output = self.run_fixture_query(
            """
            INSERT INTO closures (venue, starts_at, ends_at, reason)
            VALUES ('健身中心',
                    TIMESTAMPTZ '2026-05-19T00:00:00+08:00',
                    TIMESTAMPTZ '2026-05-20T00:00:00+08:00',
                    'Test closure');

            SELECT current_count, historical_median_current_count, sample_count
            FROM current_vs_history
            WHERE venue = '健身中心';
            """
        )

        self.assertEqual(output, "999|30.00|2")

    def test_the_latest_reading_is_shown_even_while_the_venue_is_closed(self):
        """`latest` is not filtered; only the history it is compared against is.

        The closure now covers the latest row too. The card must still say 999
        -- during a closure the sensor reads what it reads -- while the median
        comes only from open days. An implementer who switched `latest` to the
        filtered source as well would drop 健身中心 out of the view entirely
        and this would return an empty string.
        """
        output = self.run_fixture_query(
            """
            INSERT INTO closures (venue, starts_at, ends_at, reason) VALUES
                ('健身中心', TIMESTAMPTZ '2026-05-19T00:00:00+08:00',
                             TIMESTAMPTZ '2026-05-20T00:00:00+08:00', 'Test closure'),
                ('健身中心', TIMESTAMPTZ '2026-06-23T00:00:00+08:00',
                             TIMESTAMPTZ '2026-06-24T00:00:00+08:00', 'Test closure');

            SELECT current_count, historical_median_current_count, sample_count
            FROM current_vs_history
            WHERE venue = '健身中心';
            """
        )

        self.assertEqual(output, "999|30.00|2")


    def test_the_public_holiday_seed_closes_both_named_venues(self):
        """2026-06-19 端午節 shuts the two named venues and nothing else.

        Before the seed this returns all three venues. 壁球室 is the guard: a
        seed written as "every venue on that date" would swallow it, and no
        one has checked whether the squash courts were shut that day.
        """
        output = self.run_fixture_query(
            """
            SELECT venue, count(*)
            FROM occupancy_excluding_closures
            WHERE (fetched_at AT TIME ZONE 'Asia/Taipei')::date = DATE '2026-06-19'
            GROUP BY venue
            ORDER BY venue;
            """
        )

        self.assertEqual(output.splitlines(), ["壁球室|1"])

    def test_the_public_holiday_seed_is_a_whole_day_and_half_open(self):
        """23:59 on the 19th is closed; midnight starting the 20th is not."""
        output = self.run_fixture_query(
            """
            SELECT
                (SELECT count(*) FROM closure_periods
                 WHERE venue = '室內游泳池'
                   AND starts_at <= TIMESTAMPTZ '2026-06-19T23:59:00+08'
                   AND TIMESTAMPTZ '2026-06-19T23:59:00+08' < ends_at),
                (SELECT count(*) FROM closure_periods
                 WHERE venue = '室內游泳池'
                   AND starts_at <= TIMESTAMPTZ '2026-06-20T00:00:00+08'
                   AND TIMESTAMPTZ '2026-06-20T00:00:00+08' < ends_at);
            """
        )

        self.assertEqual(output, "1|0")

    def test_the_renovation_seed_is_present_and_half_open(self):
        """The seeded range covers 20 September and stops before the 21st."""
        output = self.run_fixture_query(
            """
            SELECT
                (SELECT count(*) FROM closure_periods
                 WHERE venue = '健身中心'
                   AND starts_at <= TIMESTAMPTZ '2026-09-20T23:59:00+08'
                   AND TIMESTAMPTZ '2026-09-20T23:59:00+08' < ends_at),
                (SELECT count(*) FROM closure_periods
                 WHERE venue = '健身中心'
                   AND starts_at <= TIMESTAMPTZ '2026-09-21T00:00:00+08'
                   AND TIMESTAMPTZ '2026-09-21T00:00:00+08' < ends_at);
            """
        )

        self.assertEqual(output, "1|0")


if __name__ == "__main__":
    unittest.main()
