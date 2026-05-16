# Grafana Weekly Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build PostgreSQL views, tests, and Grafana-ready SQL/documentation for a weekly gym occupancy dashboard.

**Architecture:** Keep `occupancy` as the raw source table. Add focused SQL views in `sql/grafana_weekly_views.sql`, verify them with integration-style PostgreSQL tests, then document how to apply the views and create Grafana panels against Docker PostgreSQL/Grafana.

**Tech Stack:** Python standard library `unittest`, PostgreSQL 18 SQL, Grafana PostgreSQL datasource, Docker containers `gym-postgres` and `gym-grafana`.

---

### Task 1: Add Weekly Display Views

**Files:**
- Create: `sql/grafana_weekly_views.sql`

- [ ] **Step 1: Create the SQL directory**

Run:

```bash
mkdir -p sql
```

Expected: `sql/` exists.

- [ ] **Step 2: Add the view SQL**

Create `sql/grafana_weekly_views.sql` with exactly this content:

```sql
CREATE OR REPLACE VIEW weekly_occupancy_slots AS
WITH localized AS (
    SELECT
        venue,
        fetched_at,
        fetched_at AT TIME ZONE 'Asia/Taipei' AS local_fetched_at,
        current_count,
        comfortable_count,
        capacity_count,
        CASE
            WHEN comfortable_count IS NOT NULL AND comfortable_count > 0
                THEN current_count::numeric / comfortable_count
            WHEN capacity_count IS NOT NULL AND capacity_count > 0
                THEN current_count::numeric / capacity_count
            ELSE NULL
        END AS crowding_ratio
    FROM occupancy
),
slotted AS (
    SELECT
        venue,
        fetched_at,
        current_count,
        crowding_ratio,
        EXTRACT(ISODOW FROM local_fetched_at)::integer AS weekday_number,
        TO_CHAR(local_fetched_at, 'Dy') AS weekday_label,
        MAKE_TIME(
            EXTRACT(HOUR FROM local_fetched_at)::integer,
            (FLOOR(EXTRACT(MINUTE FROM local_fetched_at) / 30)::integer * 30),
            0
        ) AS slot_start_time,
        TO_CHAR(
            MAKE_TIME(
                EXTRACT(HOUR FROM local_fetched_at)::integer,
                (FLOOR(EXTRACT(MINUTE FROM local_fetched_at) / 30)::integer * 30),
                0
            ),
            'HH24:MI'
        ) AS slot_label
    FROM localized
),
open_slots AS (
    SELECT *
    FROM slotted
    WHERE
        (
            weekday_number BETWEEN 1 AND 5
            AND slot_start_time >= TIME '08:00'
            AND slot_start_time < TIME '22:00'
        )
        OR (
            weekday_number = 6
            AND slot_start_time >= TIME '09:00'
            AND slot_start_time < TIME '22:00'
        )
        OR (
            weekday_number = 7
            AND slot_start_time >= TIME '09:00'
            AND slot_start_time < TIME '18:00'
        )
)
SELECT
    venue,
    weekday_number,
    weekday_label,
    slot_start_time,
    slot_label,
    ROUND(AVG(current_count)::numeric, 2) AS avg_current_count,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY current_count)::numeric, 2) AS median_current_count,
    MIN(current_count) AS min_current_count,
    MAX(current_count) AS max_current_count,
    COUNT(*) AS sample_count,
    ROUND(AVG(crowding_ratio)::numeric, 4) AS avg_crowding_ratio
FROM open_slots
GROUP BY venue, weekday_number, weekday_label, slot_start_time, slot_label;

CREATE OR REPLACE VIEW current_vs_history AS
WITH latest AS (
    SELECT DISTINCT ON (venue)
        venue,
        fetched_at,
        current_count,
        comfortable_count,
        capacity_count
    FROM occupancy
    ORDER BY venue, fetched_at DESC
),
latest_slotted AS (
    SELECT
        venue,
        fetched_at AS latest_fetched_at,
        current_count,
        CASE
            WHEN comfortable_count IS NOT NULL AND comfortable_count > 0
                THEN current_count::numeric / comfortable_count
            WHEN capacity_count IS NOT NULL AND capacity_count > 0
                THEN current_count::numeric / capacity_count
            ELSE NULL
        END AS current_crowding_ratio,
        EXTRACT(ISODOW FROM fetched_at AT TIME ZONE 'Asia/Taipei')::integer AS weekday_number,
        MAKE_TIME(
            EXTRACT(HOUR FROM fetched_at AT TIME ZONE 'Asia/Taipei')::integer,
            (FLOOR(EXTRACT(MINUTE FROM fetched_at AT TIME ZONE 'Asia/Taipei') / 30)::integer * 30),
            0
        ) AS slot_start_time
    FROM latest
),
history AS (
    SELECT
        l.venue,
        l.latest_fetched_at,
        l.current_count,
        l.current_crowding_ratio,
        h.current_count AS historical_current_count
    FROM latest_slotted l
    JOIN occupancy h
        ON h.venue = l.venue
       AND h.fetched_at <> l.latest_fetched_at
       AND EXTRACT(ISODOW FROM h.fetched_at AT TIME ZONE 'Asia/Taipei')::integer = l.weekday_number
       AND MAKE_TIME(
            EXTRACT(HOUR FROM h.fetched_at AT TIME ZONE 'Asia/Taipei')::integer,
            (FLOOR(EXTRACT(MINUTE FROM h.fetched_at AT TIME ZONE 'Asia/Taipei') / 30)::integer * 30),
            0
       ) = l.slot_start_time
)
SELECT
    venue,
    latest_fetched_at,
    current_count,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY historical_current_count)::numeric, 2) AS historical_median_current_count,
    ROUND(AVG(historical_current_count)::numeric, 2) AS historical_avg_current_count,
    ROUND((current_count - PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY historical_current_count))::numeric, 2) AS difference_from_median,
    COUNT(*) AS sample_count,
    ROUND(current_crowding_ratio::numeric, 4) AS current_crowding_ratio
FROM history
GROUP BY venue, latest_fetched_at, current_count, current_crowding_ratio;
```

- [ ] **Step 3: Apply the views to local Docker PostgreSQL**

Run:

```bash
psql postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch -v ON_ERROR_STOP=1 -f sql/grafana_weekly_views.sql
```

Expected: `CREATE VIEW` appears twice.

- [ ] **Step 4: Commit**

Run:

```bash
git add sql/grafana_weekly_views.sql
git commit -m "feat: add grafana weekly occupancy views"
```

Expected: commit succeeds.

### Task 2: Add PostgreSQL View Tests

**Files:**
- Create: `test_grafana_weekly_views.py`
- Modify: `requirements.txt` only if a future implementation chooses a test dependency; the default plan uses no new dependency.

- [ ] **Step 1: Write the tests**

Create `test_grafana_weekly_views.py` with exactly this content:

```python
import os
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
    return subprocess.run(
        ["psql", POSTGRES_URL, "-v", "ON_ERROR_STOP=1", "-At"],
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
```

- [ ] **Step 2: Run tests**

Run:

```bash
python3 -m unittest test_grafana_weekly_views.py test_sync_to_postgres.py
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

Run:

```bash
git add test_grafana_weekly_views.py
git commit -m "test: cover grafana weekly views"
```

Expected: commit succeeds.

### Task 3: Document Grafana Panel SQL

**Files:**
- Modify: `README_Local.md`

- [ ] **Step 1: Add an apply-views command**

In `README_Local.md`, under `## Visualize With Grafana and PostgreSQL`, add this after the sync command:

```markdown
Apply the weekly dashboard views:

```bash
psql postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch \
  -v ON_ERROR_STOP=1 \
  -f sql/grafana_weekly_views.sql
```
```

- [ ] **Step 2: Add weekly dashboard panel SQL**

In `README_Local.md`, under `### Create Grafana Panels`, add this section before the existing time-series examples:

```markdown
Weekly 30-minute heatmap table:

```sql
SELECT
  weekday_label,
  slot_label,
  CASE
    WHEN '${metric}' = 'crowding_ratio' THEN avg_crowding_ratio
    ELSE avg_current_count
  END AS value,
  sample_count
FROM weekly_occupancy_slots
WHERE venue IN (${venue:sqlstring})
ORDER BY slot_start_time, weekday_number;
```

Current vs same weekday/time history:

```sql
SELECT
  venue,
  latest_fetched_at,
  current_count,
  historical_median_current_count,
  difference_from_median,
  sample_count,
  current_crowding_ratio
FROM current_vs_history
ORDER BY venue;
```

Selected venue exact-count line chart:

```sql
SELECT
  fetched_at AS "time",
  venue AS metric,
  current_count AS value
FROM occupancy
WHERE $__timeFilter(fetched_at)
  AND venue IN (${venue:sqlstring})
ORDER BY fetched_at;
```
```

- [ ] **Step 3: Add Grafana variables**

In the same README section, add:

```markdown
Suggested dashboard variables:

- `venue`: query variable, `SELECT DISTINCT venue FROM occupancy ORDER BY venue;`, enable multi-value.
- `metric`: custom variable, values `avg_count,crowding_ratio`, default `avg_count`.
```

- [ ] **Step 4: Commit**

Run:

```bash
git add README_Local.md
git commit -m "docs: add grafana weekly dashboard setup"
```

Expected: commit succeeds.

### Task 4: Verify Against Running Docker Stack

**Files:**
- No file changes required unless verification reveals a documented command is wrong.

- [ ] **Step 1: Confirm containers**

Run:

```bash
docker ps --filter name=gym-postgres --filter name=gym-grafana
```

Expected: both `gym-postgres` and `gym-grafana` are `Up`.

- [ ] **Step 2: Sync latest SQLite data**

Run:

```bash
python3 sync_to_postgres.py \
  --sqlite-db gym_counts.sqlite3 \
  --postgres-url postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch
```

Expected: output starts with `synced` and reports a positive row count.

- [ ] **Step 3: Apply views**

Run:

```bash
psql postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch \
  -v ON_ERROR_STOP=1 \
  -f sql/grafana_weekly_views.sql
```

Expected: `CREATE VIEW` appears twice.

- [ ] **Step 4: Inspect view output**

Run:

```bash
psql postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch -c \
  "SELECT venue, weekday_label, slot_label, avg_current_count, sample_count FROM weekly_occupancy_slots ORDER BY venue, weekday_number, slot_start_time LIMIT 10;"
```

Expected: rows appear with venue names, weekday labels, 30-minute slot labels, average counts, and sample counts.

- [ ] **Step 5: Inspect current summary output**

Run:

```bash
psql postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch -c \
  "SELECT venue, latest_fetched_at, current_count, historical_median_current_count, difference_from_median, sample_count FROM current_vs_history ORDER BY venue;"
```

Expected: each venue with enough history for its latest slot appears. Venues without prior matching history may be absent in this first version.

### Task 5: Decide Grafana Creation Path

**Files:**
- Optional Create: `grafana/weekly-dashboard.json` if the API/provisioning path is chosen.
- Optional Modify: `README_Local.md` if manual GUI steps need more detail after testing.

- [ ] **Step 1: Try API access with default credentials**

Run:

```bash
curl -s -u admin:admin http://127.0.0.1:3000/api/health
```

Expected if default credentials still work: JSON containing `"database":"ok"`.

- [ ] **Step 2: If API works, create a dashboard JSON in a follow-up task**

Use Grafana API/provisioning to create panels from the SQL in Task 3. Keep the JSON generated and committed as `grafana/weekly-dashboard.json`.

- [ ] **Step 3: If API does not work, use Web GUI for panel creation**

Open `http://localhost:3000`, log in manually, then create:

- top table/stat panel from `current_vs_history`
- main table panel from `weekly_occupancy_slots` with colored background thresholds
- lower time-series panel from `occupancy`

Expected: the dashboard can be built manually using the documented SQL even without API credentials.
