"""Integration tests for the public API surface of sql/migrations/005.

DESTRUCTIVE: creates and drops a database. Guarded by dbtest_support, which
requires TEST_POSTGRES_URL plus ALLOW_DESTRUCTIVE_DB_TESTS=1 and refuses
managed hosts.

    TEST_POSTGRES_URL=postgresql://postgres@127.0.0.1:55437/postgres \\
    ALLOW_DESTRUCTIVE_DB_TESTS=1 \\
        python3 -m unittest test_public_api_migration

Why a database rather than a schema, unlike the other DB tests: 005 and the
files it builds on write into `public` by name, so there is nothing to scope a
search_path to. The database name still carries the per-process suffix.

**Roles are deliberately not applied.** 003 runs `ALTER ROLE gym_writer
PASSWORD ...`, which is cluster-wide -- pointing this at a developer's own
Postgres would silently reset the passwords of their real collector roles. So
this applies 001, 002 and 005 only, the same three docker-compose.yml mounts
into docker-entrypoint-initdb.d, and 005 is written to skip every grant whose
role is absent. What that leaves uncovered is the revoke matrix itself, which
has to be checked against the real anon role and a live PostgREST anyway --
and there, paired with a positive control, because a keyless request is also
a 401.
"""
import json
import subprocess
import unittest
from pathlib import Path

from dbtest_support import (TEST_POSTGRES_URL, destructive_tests_blocked,
                            run_psql, scoped_name, with_database)


HERE = Path(__file__).parent
MIGRATIONS = [HERE / "sql" / "migrations" / name for name in
              ("001_occupancy.sql", "002_views.sql", "005_public_api.sql")]

TEST_DB = scoped_name("gym_api")
BLOCKED = destructive_tests_blocked()


@unittest.skipIf(BLOCKED, BLOCKED or "")
class PublicApiMigrationTests(unittest.TestCase):
    """One database, migrations applied twice, shared by every test here."""

    @classmethod
    def setUpClass(cls):
        run_psql(f'DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE);'
                 f'CREATE DATABASE {TEST_DB};')
        cls.url = with_database(TEST_POSTGRES_URL, TEST_DB)
        # Twice, in one setUp, so every assertion below runs against a database
        # that has seen the migrations more than once. R4: re-applying must not
        # duplicate a seed or overwrite a tuned limit.
        cls.census = []
        for _ in range(2):
            subprocess.run([str(HERE / "tools" / "apply_migrations.sh"),
                            "--skip-roles", cls.url],
                           check=True, capture_output=True, text=True)
            cls.census.append(cls.counts())

    @classmethod
    def tearDownClass(cls):
        run_psql(f'DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE);')

    @classmethod
    def counts(cls):
        """Every persistent table the migrations create, not a chosen two.

        Discovered rather than listed, so a table added later is censused
        without anyone remembering to add it here.
        """
        return run_psql("""
            SELECT string_agg(
                       schemaname || '.' || tablename || '=' || (
                           xpath('/row/c/text()', query_to_xml(
                               format('SELECT count(*) AS c FROM %I.%I',
                                      schemaname, tablename),
                               false, true, ''))
                       )[1]::text,
                       ' ' ORDER BY schemaname, tablename)
            FROM pg_tables
            WHERE schemaname IN ('public', 'api_private');
        """, url=cls.url)

    def sql(self, script):
        return run_psql(script, url=self.url)

    def test_applying_the_migrations_twice_changes_nothing(self):
        """R4, through tools/apply_migrations.sh itself.

        Driving the script rather than psql directly is deliberate: it is what
        catches a glob-ordering or --single-transaction regression, which a
        hand-listed file loop cannot see.
        """
        self.assertNotEqual(self.census[0].strip(), "",
                            "census came back empty -- it is proving nothing")
        self.assertEqual(self.census[0], self.census[1])

    def test_the_public_holiday_seed_survives_a_reapply(self):
        self.assertEqual(
            self.sql("SELECT count(*) FROM public.closures "
                     "WHERE reason = 'Public holiday';"), "2")

    def test_window_closure_rows_uses_overlap_not_containment(self):
        """The boundary matrix. Both edges are exclusive.

        A closure ending exactly when the window opens is over; one starting
        exactly when the window closes has not begun.
        """
        matrix = self.sql("""
            INSERT INTO public.closures (venue, starts_at, ends_at, reason)
            VALUES ('BOUNDARY', TIMESTAMPTZ '2026-03-02 10:00+08',
                                TIMESTAMPTZ '2026-03-02 12:00+08', 'Probe');
            SELECT string_agg(hit::text, ',' ORDER BY ord) FROM (
              SELECT 1 AS ord, count(*) AS hit FROM api_private.window_closure_rows(
                TIMESTAMPTZ '2026-03-02 12:00+08', TIMESTAMPTZ '2026-03-02 14:00+08')
                WHERE venue = 'BOUNDARY'
              UNION ALL SELECT 2, count(*) FROM api_private.window_closure_rows(
                TIMESTAMPTZ '2026-03-02 08:00+08', TIMESTAMPTZ '2026-03-02 10:00+08')
                WHERE venue = 'BOUNDARY'
              UNION ALL SELECT 3, count(*) FROM api_private.window_closure_rows(
                TIMESTAMPTZ '2026-03-02 11:00+08', TIMESTAMPTZ '2026-03-02 13:00+08')
                WHERE venue = 'BOUNDARY'
              UNION ALL SELECT 4, count(*) FROM api_private.window_closure_rows(
                TIMESTAMPTZ '2026-03-02 09:00+08', TIMESTAMPTZ '2026-03-02 11:00+08')
                WHERE venue = 'BOUNDARY'
              UNION ALL SELECT 5, count(*) FROM api_private.window_closure_rows(
                TIMESTAMPTZ '2026-03-02 10:30+08', TIMESTAMPTZ '2026-03-02 11:00+08')
                WHERE venue = 'BOUNDARY'
              UNION ALL SELECT 6, count(*) FROM api_private.window_closure_rows(
                TIMESTAMPTZ '2026-03-02 08:00+08', TIMESTAMPTZ '2026-03-02 14:00+08')
                WHERE venue = 'BOUNDARY'
            ) t;
        """)
        # ends-at-open, starts-at-close, straddle end, straddle start,
        # window inside closure, closure inside window.
        self.assertEqual(matrix, "0,0,1,1,1,1")

    def test_a_windowed_closure_is_returned_unclipped(self):
        """The caller clamps to its own readings, so the API must not clamp."""
        self.assertEqual(
            self.sql("""
                INSERT INTO public.closures (venue, starts_at, ends_at, reason)
                VALUES ('UNCLIPPED', TIMESTAMPTZ '2026-03-02 10:00+08',
                                     TIMESTAMPTZ '2026-03-02 12:00+08', 'Probe');
                SELECT to_char(starts_at AT TIME ZONE 'Asia/Taipei', 'HH24:MI')
                       || '-' ||
                       to_char(ends_at AT TIME ZONE 'Asia/Taipei', 'HH24:MI')
                FROM api_private.window_closure_rows(
                       TIMESTAMPTZ '2026-03-02 10:30+08',
                       TIMESTAMPTZ '2026-03-02 11:00+08')
                WHERE venue = 'UNCLIPPED';
            """),
            "10:00-12:00")

    def test_the_two_closure_helpers_answer_different_questions(self):
        """The opening instant: shut according to one, not overlapped by the other.

        This is the whole reason window_closure_rows exists rather than a flag
        on active_closure_rows. Both sides use ranges that actually occur --
        `gym_live` never asks for a zero-width window -- so a 1,1 here means
        the two predicates have been collapsed and R6 is silently gone.
        """
        self.assertEqual(
            self.sql("""
                INSERT INTO public.closures (venue, starts_at, ends_at, reason)
                VALUES ('SAME_INSTANT', TIMESTAMPTZ '2026-03-02 10:00+08',
                                        TIMESTAMPTZ '2026-03-02 12:00+08', 'Probe');
                SELECT (SELECT count(*) FROM api_private.active_closure_rows(
                          TIMESTAMPTZ '2026-03-02 10:00+08')
                        WHERE venue = 'SAME_INSTANT') || ',' ||
                       (SELECT count(*) FROM api_private.window_closure_rows(
                          TIMESTAMPTZ '2026-03-02 08:00+08',
                          TIMESTAMPTZ '2026-03-02 10:00+08')
                        WHERE venue = 'SAME_INSTANT');
            """),
            "1,0")

    def test_gym_live_carries_both_closure_keys_with_one_shape(self):
        """The page has one parser; both arrays must fit it."""
        payload = json.loads(self.sql("SELECT public.gym_live(7)::text;"))
        self.assertIn("activeClosures", payload)
        self.assertIn("windowClosures", payload)
        self.assertIsInstance(payload["windowClosures"], list)
        self.assertIsInstance(payload["activeClosures"], list)

    def test_gym_live_window_closures_use_the_documented_element_shape(self):
        """venue/from/to/reason/source -- what closureBands() reads.

        The closure is anchored to now() rather than to the renovation seed:
        that seed ends on 2026-09-20, and a test resting on it would start
        failing on its own one day with nothing actually broken.
        """
        payload = json.loads(self.sql("""
            INSERT INTO public.occupancy
                (fetched_at, source_updated_at, venue, current_count,
                 comfortable_count, capacity_count)
            VALUES (now(), now(), 'SHAPE', 5, 80, 161);
            INSERT INTO public.closures (venue, starts_at, ends_at, reason, source)
            VALUES ('SHAPE', now() - INTERVAL '1 hour',
                             now() + INTERVAL '1 hour', 'Probe', NULL);
            SELECT public.gym_live(7)::text;
        """))
        shaped = [c for c in payload["windowClosures"] if c["venue"] == "SHAPE"]
        self.assertEqual(len(shaped), 1, payload["windowClosures"])
        self.assertEqual(sorted(shaped[0]),
                         ["from", "reason", "source", "to", "venue"])

    def test_window_closures_arrive_ordered_by_start(self):
        """The page labels a band with the first closure covering it.

        Two closures over the same readings -- the real 2026-08-24 shape --
        must arrive enclosing-first, or the band gets the nested closure's
        reason. jsonb_agg does not inherit the set-returning function's own
        ORDER BY, so this fails if the aggregate loses its ordering.
        """
        payload = json.loads(self.sql("""
            INSERT INTO public.occupancy
                (fetched_at, source_updated_at, venue, current_count,
                 comfortable_count, capacity_count)
            VALUES (now(), now(), 'ORDERED', 5, 80, 161);
            INSERT INTO public.closures (venue, starts_at, ends_at, reason, source)
            VALUES ('ORDERED', now() - INTERVAL '2 hours',
                               now() + INTERVAL '2 hours', 'Outer', NULL),
                   ('ORDERED', now() - INTERVAL '1 hour',
                               now() + INTERVAL '1 hour', 'Inner', NULL);
            SELECT public.gym_live(7)::text;
        """))
        reasons = [c["reason"] for c in payload["windowClosures"]
                   if c["venue"] == "ORDERED"]
        self.assertEqual(reasons, ["Outer", "Inner"])


if __name__ == "__main__":
    unittest.main()
