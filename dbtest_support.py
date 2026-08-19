"""Guardrails for the destructive PostgreSQL integration tests.

These tests DROP schemas, CREATE and DROP roles, and DROP OWNED BY. Pointing
them at the wrong database destroys data. Three things stand between them and
that outcome:

  1. A dedicated variable. They read TEST_POSTGRES_URL, never the general
     POSTGRES_URL that sync_to_postgres.py, tools/ and the docs all use -- an
     exported POSTGRES_URL for real work must never arm a destructive test.
  2. An explicit opt-in, ALLOW_DESTRUCTIVE_DB_TESTS=1, so a URL alone is not
     enough.
  3. A refusal to touch managed database hosts, overridable only by a second,
     separately named variable.

Every schema and role name is also suffixed per process, so a stray object
from an interrupted run can never collide with -- or be mistaken for -- one
that belongs to the database.

Not named test_*.py on purpose: `unittest discover -p 'test_*.py'` would
otherwise import it as an (empty) test module.
"""
import os
import subprocess
import urllib.parse
import uuid


TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

# Hosts that are almost certainly somebody's real database.
MANAGED_HOST_MARKERS = (
    ".supabase.com", ".supabase.co", ".rds.amazonaws.com", ".neon.tech",
    ".render.com", ".azure.com", ".cloudsql", ".pooler.",
)

# One suffix per process: unique object names, and a leftover is traceable.
RUN_SUFFIX = uuid.uuid4().hex[:10]


def _managed_host(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return next((m for m in MANAGED_HOST_MARKERS if m in host), None)


def destructive_tests_blocked():
    """Return why destructive DB tests must not run, or None if they may."""
    if not TEST_POSTGRES_URL:
        return ("TEST_POSTGRES_URL is unset (POSTGRES_URL is deliberately "
                "ignored: these tests drop schemas and roles)")

    if os.environ.get("ALLOW_DESTRUCTIVE_DB_TESTS") != "1":
        return "ALLOW_DESTRUCTIVE_DB_TESTS=1 is required to run destructive DB tests"

    marker = _managed_host(TEST_POSTGRES_URL)
    if marker and os.environ.get("ALLOW_DESTRUCTIVE_DB_TESTS_ON_REMOTE") != "1":
        return (f"refusing to run destructive tests against a managed host "
                f"(matched {marker!r}); set "
                f"ALLOW_DESTRUCTIVE_DB_TESTS_ON_REMOTE=1 only if that database "
                f"is genuinely disposable")

    try:
        run_psql("SELECT 1;")
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as error:
        return f"cannot reach {TEST_POSTGRES_URL}: {error}"

    return None


def scoped_name(prefix):
    """A schema/role name unique to this process."""
    return f"{prefix}_{RUN_SUFFIX}"


def run_psql(script, url=None, extra_args=()):
    """Run a script through psql. -X so a local .psqlrc cannot change output."""
    return subprocess.run(
        ["psql", url or TEST_POSTGRES_URL, "-X", "-v", "ON_ERROR_STOP=1",
         "-qAt", *extra_args],
        input=script, text=True, check=True, capture_output=True,
    ).stdout.strip()


def with_database(url, database):
    """The same server, a different database. For tests that need `public`.

    A schema suffix is enough when a test owns only its own objects, but the
    migrations write into `public` by name, so those tests need a throwaway
    database instead.
    """
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{database}"))


def with_search_path(url, schema):
    """Point a URL at one schema using libpq connection options."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query)
    query.append(("options", f"-csearch_path={schema}"))
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query)))
