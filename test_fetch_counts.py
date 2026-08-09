import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

import fetch_counts
from fetch_counts import TAIPEI


# Anchor dates whose weekday is confirmed by the recovered 2026-08 data.
MONDAY = (2026, 8, 3)
SATURDAY = (2026, 8, 1)
SUNDAY = (2026, 8, 2)


def taipei(date_parts, hour, minute=0):
    return datetime(*date_parts, hour, minute, tzinfo=TAIPEI)


class OpeningHoursTests(unittest.TestCase):
    """Boundaries of is_open_time().

    Weekday opening is 06:00, not the 08:00 the sports centre's website lists
    for the building -- the gym itself is populated from 06:00 every weekday in
    the collected history, and commit 28a4ae6 corrected this deliberately.
    """

    def assert_open(self, moment, expected):
        self.assertIs(
            fetch_counts.is_open_time(moment), expected,
            f"{moment.isoformat()} ({moment:%a}) should be "
            f"{'open' if expected else 'closed'}")

    def test_weekday_opens_at_six_and_closes_at_twentytwo(self):
        self.assert_open(taipei(MONDAY, 5, 59), False)
        self.assert_open(taipei(MONDAY, 6, 0), True)
        self.assert_open(taipei(MONDAY, 21, 59), True)
        self.assert_open(taipei(MONDAY, 22, 0), False)

    def test_saturday_opens_at_nine_and_closes_at_twentytwo(self):
        self.assert_open(taipei(SATURDAY, 8, 59), False)
        self.assert_open(taipei(SATURDAY, 9, 0), True)
        self.assert_open(taipei(SATURDAY, 21, 59), True)
        self.assert_open(taipei(SATURDAY, 22, 0), False)

    def test_sunday_opens_at_nine_and_closes_at_eighteen(self):
        self.assert_open(taipei(SUNDAY, 8, 59), False)
        self.assert_open(taipei(SUNDAY, 9, 0), True)
        self.assert_open(taipei(SUNDAY, 17, 59), True)
        self.assert_open(taipei(SUNDAY, 18, 0), False)

    def test_opening_hour_matches_is_open_time(self):
        self.assertEqual(fetch_counts.opening_hour(taipei(MONDAY, 12)), 6)
        self.assertEqual(fetch_counts.opening_hour(taipei(SATURDAY, 12)), 9)
        self.assertEqual(fetch_counts.opening_hour(taipei(SUNDAY, 12)), 9)


class TimezoneTests(unittest.TestCase):
    def test_now_taipei_is_always_utc_plus_eight(self):
        self.assertEqual(fetch_counts.now_taipei().utcoffset(), timedelta(hours=8))

    def test_timezone_does_not_follow_the_host(self):
        # The whole point: a GitHub Actions runner is UTC. Reading the host
        # clock there would make --open-hours-only skip nearly every scheduled
        # run, silently. Run in a subprocess so TZ does not leak into this one.
        env = dict(os.environ, TZ="UTC")
        result = subprocess.run(
            [sys.executable, "-c",
             "import fetch_counts as f; print(f.now_taipei().isoformat())"],
            env=env, capture_output=True, text=True, check=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )

        self.assertTrue(
            result.stdout.strip().endswith("+08:00"),
            f"expected a Taipei offset under TZ=UTC, got {result.stdout.strip()}")

    def test_open_hours_decided_in_taipei_terms_under_a_utc_host(self):
        # 2026-08-03 07:00 Taipei is 2026-08-02 23:00 UTC: a Sunday evening,
        # which reads as closed if the host timezone wins. It is a Monday
        # morning in Taipei, and the gym is open.
        env = dict(os.environ, TZ="UTC")
        result = subprocess.run(
            [sys.executable, "-c",
             "import fetch_counts as f;"
             "from datetime import datetime;"
             "print(f.is_open_time(datetime(2026, 8, 3, 7, 0, tzinfo=f.TAIPEI)))"],
            env=env, capture_output=True, text=True, check=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )

        self.assertEqual(result.stdout.strip(), "True")


class SourceTimestampTests(unittest.TestCase):
    def test_naive_page_timestamp_is_read_as_taipei_local(self):
        parsed = fetch_counts.parse_source_timestamp("2026-08-09 10:05")

        self.assertEqual(parsed, taipei((2026, 8, 9), 10, 5))
        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))

    def test_missing_or_malformed_timestamps_return_none(self):
        for value in (None, "", "not a timestamp", "2026-08-09T10:05:00+08:00"):
            self.assertIsNone(fetch_counts.parse_source_timestamp(value))


class ValidateRowsTests(unittest.TestCase):
    FETCHED_AT = taipei((2026, 8, 9), 10, 5)
    EXPECTED = {"健身中心", "室內游泳池"}

    def rows(self, **overrides):
        base = [
            {"venue": "健身中心", "current_count": 22, "comfortable_count": 80,
             "capacity_count": 161, "source_updated_at": "2026-08-09 10:05"},
            {"venue": "室內游泳池", "current_count": 9, "comfortable_count": 50,
             "capacity_count": 130, "source_updated_at": "2026-08-09 10:05"},
        ]
        for row in base:
            row.update(overrides)
        return base

    def test_a_complete_fresh_batch_has_no_problems(self):
        self.assertEqual(
            fetch_counts.validate_rows(self.rows(), self.FETCHED_AT, self.EXPECTED),
            [])

    def test_a_half_rendered_page_is_rejected(self):
        # The failure this exists for: the site returns one venue, the old code
        # accepts it, and the gap only surfaces months later in a chart.
        problems = fetch_counts.validate_rows(
            self.rows()[:1], self.FETCHED_AT, self.EXPECTED)

        self.assertEqual(len(problems), 1)
        self.assertIn("missing venue(s): 室內游泳池", problems[0])

    def test_an_unexpected_venue_is_reported(self):
        rows = self.rows()
        rows[0]["venue"] = "重量訓練室"

        problems = fetch_counts.validate_rows(rows, self.FETCHED_AT, self.EXPECTED)

        self.assertTrue(any("missing venue(s): 健身中心" in p for p in problems))
        self.assertTrue(any("unexpected venue(s): 重量訓練室" in p for p in problems))

    def test_without_expect_venues_the_venue_set_is_not_policed(self):
        self.assertEqual(
            fetch_counts.validate_rows(self.rows()[:1], self.FETCHED_AT), [])

    def test_negative_and_absurd_counts_are_rejected(self):
        negative = fetch_counts.validate_rows(
            self.rows(current_count=-1), self.FETCHED_AT)
        absurd = fetch_counts.validate_rows(
            self.rows(current_count=9999), self.FETCHED_AT)

        self.assertTrue(any("negative current_count" in p for p in negative))
        self.assertTrue(any("far exceeds capacity" in p for p in absurd))

    def test_crowding_beyond_comfortable_is_still_accepted(self):
        # 120 people in a room comfortable for 80 is a busy evening, not a bug.
        self.assertEqual(
            fetch_counts.validate_rows(self.rows(current_count=120),
                                       self.FETCHED_AT),
            [])

    def test_a_frozen_source_timestamp_is_rejected_once_not_per_venue(self):
        problems = fetch_counts.validate_rows(
            self.rows(source_updated_at="2026-08-09 09:00"),
            self.FETCHED_AT, self.EXPECTED)

        self.assertEqual(len(problems), 1)
        self.assertIn("65 min old", problems[0])

    def test_the_same_venue_twice_is_rejected(self):
        # The unique index on (fetched_at, venue) would silently keep one of
        # them, so a markup change that duplicates a card must fail loudly.
        rows = self.rows()
        rows[1]["venue"] = "健身中心"

        problems = fetch_counts.validate_rows(rows, self.FETCHED_AT)

        self.assertTrue(any("duplicate venue(s): 健身中心" in p for p in problems))

    def test_a_timestamp_from_the_future_is_rejected(self):
        problems = fetch_counts.validate_rows(
            self.rows(source_updated_at="2026-08-09 11:00"),
            self.FETCHED_AT, self.EXPECTED)

        self.assertEqual(len(problems), 1)
        self.assertIn("in the future", problems[0])

    def test_a_minute_of_clock_skew_is_tolerated(self):
        # Both ends round to the minute; a small lead is normal, not a fault.
        self.assertEqual(
            fetch_counts.validate_rows(
                self.rows(source_updated_at="2026-08-09 10:06"),
                self.FETCHED_AT, self.EXPECTED),
            [])

    def test_staleness_check_is_skippable_for_closed_hours_smoke_tests(self):
        self.assertEqual(
            fetch_counts.validate_rows(
                self.rows(source_updated_at="2026-08-01 09:00"),
                self.FETCHED_AT, self.EXPECTED, max_source_age_minutes=None),
            [])


class FetchRetryTests(unittest.TestCase):
    def setUp(self):
        sleep = mock.patch("fetch_counts.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_read_timeout_is_retried_rather_than_raised(self):
        # TimeoutError is not a URLError. The old `except URLError` let it
        # escape as a traceback with no retry -- unlikely on campus, likely
        # over a trans-Pacific link from a GitHub runner.
        with mock.patch("fetch_counts.fetch_html",
                        side_effect=TimeoutError("read timed out")) as fetch:
            with self.assertRaises(SystemExit):
                fetch_counts.fetch_and_save(
                    conn=None, url="https://example.invalid", retries=2)

        self.assertEqual(fetch.call_count, 3)

    def test_dropped_connection_is_retried(self):
        import http.client

        with mock.patch("fetch_counts.fetch_html",
                        side_effect=http.client.RemoteDisconnected("bye")) as fetch:
            self.assertFalse(
                fetch_counts.fetch_and_save(
                    conn=None, url="https://example.invalid",
                    exit_on_error=False, retries=1))

        self.assertEqual(fetch.call_count, 2)

    def test_a_programming_error_still_propagates(self):
        # Broadening the except clause must not turn real bugs into retries.
        with mock.patch("fetch_counts.fetch_html",
                        side_effect=ValueError("bad regex group")):
            with self.assertRaises(ValueError):
                fetch_counts.fetch_and_save(
                    conn=None, url="https://example.invalid", retries=1)

    def test_a_batch_failing_validation_is_retried_then_refused(self):
        html = "<irrelevant/>"
        half = [{"venue": "健身中心", "current_count": 22, "comfortable_count": 80,
                 "capacity_count": 161, "source_updated_at": "2026-08-09 10:05"}]

        with mock.patch("fetch_counts.fetch_html", return_value=html), \
                mock.patch("fetch_counts.parse_counts", return_value=half), \
                mock.patch("fetch_counts.save_rows") as save:
            with self.assertRaises(SystemExit):
                fetch_counts.fetch_and_save(
                    conn=None, url="https://example.invalid", retries=1,
                    fetched_at=taipei((2026, 8, 9), 10, 5),
                    expect_venues={"健身中心", "室內游泳池"})

        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
