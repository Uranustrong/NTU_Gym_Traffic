#!/usr/bin/env python3
"""Render the gym dashboard as a self-contained static HTML page.

This is the public-facing counterpart to the Grafana dashboard. Grafana on
the server is only reachable on 127.0.0.1, so it cannot be shown to outside
visitors directly. Instead this tool queries Postgres, bakes the four panels'
data into a JSON blob, embeds the *same* ECharts panel code that
`grafana/weekly-dashboard.json` uses, and writes one `index.html` that Apache
/ nginx UserDir can serve as a fully interactive (palette / venue / hover)
static page.

Run from cron every 5 minutes, e.g.:
    */5 * * * * /tmp2/<id>/Gym_Fetch/tools/render_public_html.py \\
        --output-dir /home/<id>/public_html/gym

Single source of truth: the panel rendering JS is read at runtime from
`grafana/weekly-dashboard.json`, so editing a panel in Grafana automatically
flows into the public page on the next render. Only the SQL is re-stated here
(macro-free) because the dashboard's queries use Grafana-specific macros
(`${venue:sqlstring}`, `$__timeFilter`, ...).

No third-party Python packages: data is pulled via the `psql` CLI, same as
sync_to_postgres.py.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_JSON = REPO_DIR / "grafana" / "weekly-dashboard.json"
TEMPLATE_HTML = REPO_DIR / "tools" / "public_template.html"
ECHARTS_JS = REPO_DIR / "tools" / "echarts.min.js"


# --------------------------------------------------------------------------
# .env loading + postgres URL construction (mirrors tools/sync_local.sh)
# --------------------------------------------------------------------------
def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = REPO_DIR / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        env[k] = v
    return env


def build_postgres_url(env: dict[str, str]) -> str:
    if env.get("POSTGRES_URL"):
        return env["POSTGRES_URL"]
    user = urllib.parse.quote(env.get("POSTGRES_USER", "songhejun"), safe="")
    password = urllib.parse.quote(env.get("POSTGRES_PASSWORD", "gym_fetch_dev"), safe="")
    db = env.get("POSTGRES_DB", "gym_fetch")
    return f"postgresql://{user}:{password}@127.0.0.1:5433/{db}"


# --------------------------------------------------------------------------
# query execution
# --------------------------------------------------------------------------
def run_query(sql: str, postgres_url: str, psql_path: str) -> list[dict[str, str]]:
    """Run SQL through psql, return rows as a list of {column: value} dicts."""
    proc = subprocess.run(
        [psql_path, postgres_url, "--csv", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, check=True,
    )
    reader = csv.DictReader(io.StringIO(proc.stdout))
    return list(reader)


def pg_quote(value: str) -> str:
    """Single-quote a SQL string literal, escaping embedded quotes."""
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# field typing -> Grafana-style series ({fields: [{name, type, values}]})
# --------------------------------------------------------------------------
def _coerce(value: str, kind: str):
    if value == "" or value is None:
        return None
    if kind == "int":
        return int(float(value))
    if kind == "number":
        return float(value)
    return value  # string / time(epoch already numeric upstream)


def to_series(rows: list[dict[str, str]], field_specs: list[tuple[str, str, str]]):
    """field_specs: list of (column_name, grafana_type, coerce_kind)."""
    fields = []
    for col, gtype, kind in field_specs:
        values = [_coerce(r.get(col, ""), kind) for r in rows]
        fields.append({"name": col, "type": gtype, "values": values})
    return {"fields": fields}


# --------------------------------------------------------------------------
# SQL (macro-free restatements of the dashboard panel queries)
# --------------------------------------------------------------------------
def sql_panel1(venue: str) -> str:
    v = pg_quote(venue)
    return f"""
WITH today AS (
  SELECT EXTRACT(ISODOW FROM now() AT TIME ZONE 'Asia/Taipei')::int AS wd
),
hourly_typical AS (
  SELECT EXTRACT(HOUR FROM slot_start_time)::int AS hour,
         ROUND(AVG(avg_current_count))::int      AS typical_count,
         SUM(sample_count)                       AS samples
  FROM weekly_occupancy_slots, today
  WHERE venue = {v} AND weekday_number = today.wd
  GROUP BY hour
),
current_now AS (
  SELECT DISTINCT ON (venue)
    EXTRACT(HOUR FROM fetched_at AT TIME ZONE 'Asia/Taipei')::int AS hour,
    current_count
  FROM occupancy WHERE venue = {v}
  ORDER BY venue, fetched_at DESC
)
SELECT 'typical'::text AS kind, hour, typical_count AS count, samples FROM hourly_typical
UNION ALL
SELECT 'now'::text, hour, current_count, 1 FROM current_now
ORDER BY kind DESC, hour;
"""


def sql_panel2(venue: str, granularity: str) -> str:
    v = pg_quote(venue)
    g = pg_quote(granularity)
    return f"""
WITH bucketed AS (
  SELECT weekday_number,
    CASE WHEN {g} = '60min'
      THEN to_char(date_trunc('hour', ('2000-01-01 '||slot_start_time)::timestamp), 'HH24:00')
      ELSE slot_label END AS slot_label,
    CASE WHEN {g} = '60min'
      THEN date_trunc('hour', ('2000-01-01 '||slot_start_time)::timestamp)::time
      ELSE slot_start_time END AS slot_start_time,
    avg_current_count, avg_crowding_ratio, sample_count
  FROM weekly_occupancy_slots
  WHERE venue = {v}
)
SELECT weekday_number, slot_label, slot_start_time,
  AVG(avg_current_count)  AS avg_count,
  AVG(avg_crowding_ratio) AS crowding_ratio,
  SUM(sample_count)       AS sample_count
FROM bucketed
GROUP BY weekday_number, slot_label, slot_start_time
ORDER BY slot_start_time, weekday_number;
"""


def sql_panel3(venue: str, days: int) -> str:
    v = pg_quote(venue)
    return f"""
WITH params AS (
  SELECT EXTRACT(DOW  FROM fetched_at AT TIME ZONE 'Asia/Taipei')::int AS dow,
         EXTRACT(HOUR FROM fetched_at AT TIME ZONE 'Asia/Taipei')::int AS hr,
         fetched_at, venue, current_count
  FROM occupancy
  WHERE fetched_at >= now() - interval '{days} days' AND venue = {v}
)
SELECT EXTRACT(EPOCH FROM fetched_at) * 1000 AS time,
       venue AS metric,
       current_count AS value
FROM params
WHERE (dow BETWEEN 1 AND 5 AND hr >= 6 AND hr < 22)
   OR (dow = 6              AND hr >= 9 AND hr < 22)
   OR (dow = 0              AND hr >= 9 AND hr < 18)
ORDER BY fetched_at;
"""


SQL_PANEL4 = """
WITH today_start AS (
  SELECT (date_trunc('day', now() AT TIME ZONE 'Asia/Taipei') AT TIME ZONE 'Asia/Taipei') AS ts
),
latest_meta AS (
  SELECT DISTINCT ON (venue) venue, comfortable_count, capacity_count
  FROM occupancy
  WHERE comfortable_count IS NOT NULL OR capacity_count IS NOT NULL
  ORDER BY venue, fetched_at DESC
),
readings_today AS (
  SELECT venue, fetched_at, current_count
  FROM occupancy, today_start
  WHERE fetched_at >= today_start.ts
)
SELECT 'summary'::text AS kind, cvh.venue,
  EXTRACT(EPOCH FROM cvh.latest_fetched_at) * 1000 AS ts_ms,
  cvh.current_count,
  cvh.historical_median_current_count AS hist_median,
  cvh.difference_from_median          AS diff,
  cvh.current_crowding_ratio          AS ratio,
  meta.comfortable_count              AS comfortable,
  meta.capacity_count                 AS capacity
FROM current_vs_history cvh
LEFT JOIN latest_meta meta ON meta.venue = cvh.venue
UNION ALL
SELECT 'reading'::text, venue,
  EXTRACT(EPOCH FROM fetched_at) * 1000,
  current_count, NULL, NULL, NULL, NULL, NULL
FROM readings_today
ORDER BY kind DESC, venue, ts_ms;
"""

SQL_VENUES = "SELECT DISTINCT venue FROM occupancy ORDER BY venue;"

PANEL1_FIELDS = [("kind", "string", "string"), ("hour", "number", "int"),
                 ("count", "number", "int"), ("samples", "number", "int")]
PANEL2_FIELDS = [("weekday_number", "number", "int"), ("slot_label", "string", "string"),
                 ("slot_start_time", "string", "string"), ("avg_count", "number", "number"),
                 ("crowding_ratio", "number", "number"), ("sample_count", "number", "int")]
PANEL3_FIELDS = [("time", "time", "number"), ("metric", "string", "string"),
                 ("value", "number", "number")]
PANEL4_FIELDS = [("kind", "string", "string"), ("venue", "string", "string"),
                 ("ts_ms", "number", "number"), ("current_count", "number", "int"),
                 ("hist_median", "number", "number"), ("diff", "number", "number"),
                 ("ratio", "number", "number"), ("comfortable", "number", "int"),
                 ("capacity", "number", "int")]


def extract_panel_js() -> dict[str, str]:
    """Pull the four panels' getOption JS bodies out of weekly-dashboard.json."""
    dash = json.loads(DASHBOARD_JSON.read_text())
    js: dict[str, str] = {}
    for panel in dash["panels"]:
        pid = str(panel["id"])
        js[pid] = panel["options"]["getOption"]
    return js


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--output-dir", required=True,
                   help="Directory to write index.html into (e.g. ~/public_html/gym).")
    p.add_argument("--postgres-url", default=None,
                   help="Override the postgres URL (default: built from .env).")
    p.add_argument("--psql", default="psql",
                   help="psql executable, or tools/psql_gym_postgres.sh to wrap via docker.")
    p.add_argument("--days", type=int, default=7,
                   help="Trailing window for the panel-3 line chart (default: 7).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    env = load_env()
    postgres_url = args.postgres_url or build_postgres_url(env)

    try:
        venue_rows = run_query(SQL_VENUES, postgres_url, args.psql)
    except subprocess.CalledProcessError as exc:
        print(f"venue query failed: {exc.stderr or exc}", file=sys.stderr)
        return 1
    venues = [r["venue"] for r in venue_rows]
    if not venues:
        print("no venues found in occupancy table", file=sys.stderr)
        return 1

    data: dict = {
        "venues": venues,
        "panel1": {}, "panel2": {}, "panel3": {},
        "panel4": None,
        "panelJs": extract_panel_js(),
    }

    try:
        for venue in venues:
            data["panel1"][venue] = to_series(
                run_query(sql_panel1(venue), postgres_url, args.psql), PANEL1_FIELDS)
            data["panel2"][venue] = {
                g: to_series(run_query(sql_panel2(venue, g), postgres_url, args.psql), PANEL2_FIELDS)
                for g in ("30min", "60min")
            }
            data["panel3"][venue] = to_series(
                run_query(sql_panel3(venue, args.days), postgres_url, args.psql), PANEL3_FIELDS)
        data["panel4"] = to_series(
            run_query(SQL_PANEL4, postgres_url, args.psql), PANEL4_FIELDS)
    except subprocess.CalledProcessError as exc:
        print(f"panel query failed: {exc.stderr or exc}", file=sys.stderr)
        return 1

    now = dt.datetime.now().astimezone()
    html = TEMPLATE_HTML.read_text()
    html = html.replace("{{DATA_JSON}}", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    html = html.replace("{{GENERATED_AT}}", now.strftime("%Y-%m-%d %H:%M %Z"))

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "index.html"
    # Write to a temp file then rename, so nginx never serves a half-written page.
    tmp_path = output_dir / ".index.html.tmp"
    tmp_path.write_text(html, encoding="utf-8")
    tmp_path.replace(out_path)

    # Ship the ECharts bundle alongside index.html so the public page is fully
    # self-contained and does not depend on an external CDN. Copy only when the
    # destination is missing or stale to avoid rewriting 1 MB every 5 minutes.
    echarts_dst = output_dir / "echarts.min.js"
    if not echarts_dst.exists() or echarts_dst.stat().st_size != ECHARTS_JS.stat().st_size:
        shutil.copy2(ECHARTS_JS, echarts_dst)
        print(f"copied {echarts_dst.name} ({ECHARTS_JS.stat().st_size:,} bytes)")

    print(f"wrote {out_path} ({len(html):,} bytes, {len(venues)} venues)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
