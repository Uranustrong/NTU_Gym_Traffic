# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 輸出語言

預設使用**繁體中文（台灣）**作為主要輸出語言，技術名詞、程式碼、檔案路徑、commit message、代碼註解仍維持英文。用戶習慣中英混用，回覆風格保持精簡、技術導向。

## Project shape

Scrapes NTU sports center occupancy from `rent.pe.ntu.edu.tw` every 5 minutes during opening hours, stores rows in a local SQLite DB, and (optionally) syncs them into a Dockerized PostgreSQL for Grafana to visualize.

Pure Python standard library — no third-party packages. `requirements.txt` is intentionally empty.

## Two deployment paths, one codebase

The same code runs in two places, and the two READMEs reflect that:

- **`README_Server.md`** — the *remote* collector at `b12902066@ws7.csie.ntu.edu.tw:/tmp2/b12902066/Gym_Fetch`, where cron runs `fetch_counts.py` on 5-minute boundaries during opening hours.
- **`README_Local.md`** — the *local* mac mirror at `/Users/songhejun/Downloads/My_Project/Fetch_Gym`, which pulls the remote SQLite via `rsync.sh` and syncs into local Docker PostgreSQL + Grafana.

`rsync.sh pull-data` is the bridge: it takes a `sqlite3 .backup` snapshot on the remote, rsyncs it down (versioning the prior local DB as `*.bak-<timestamp>`), then auto-runs `sync_to_postgres.py` if present.

## Common commands

```bash
# One-shot fetch into gym_counts.sqlite3 (no logging to file)
python3 fetch_counts.py --no-log-file

# Long-running, aligned-to-5min, skips closed hours
python3 fetch_counts.py --loop --open-hours-only

# Sync SQLite → Postgres (Docker setup uses port 5433 on host)
python3 sync_to_postgres.py \
  --sqlite-db gym_counts.sqlite3 \
  --postgres-url postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch

# Same, but when host has no psql — wrap through Docker
python3 sync_to_postgres.py \
  --sqlite-db gym_counts.sqlite3 \
  --postgres-url postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch \
  --psql tools/psql_gym_postgres.sh

# Apply Grafana SQL views into Postgres
docker exec -i gym-postgres psql -U songhejun -d gym_fetch \
  -v ON_ERROR_STOP=1 -f - < sql/grafana_weekly_views.sql

# Bring up the whole stack (Grafana + Postgres) on a clean machine
cp .env.example .env  # then edit passwords
docker compose up -d

# Pull data + logs down from remote and re-sync into Postgres
./rsync.sh pull-data

# Tests
python3 -m unittest test_sync_to_postgres.py
python3 -m unittest test_grafana_weekly_views.py   # requires running gym-postgres
python3 -m unittest test_grafana_weekly_views.GrafanaWeeklyViewsTests.test_weekly_slots_bucket_30_minutes_and_open_hours
```

`test_grafana_weekly_views.py` is an **integration test** — it requires either a local `psql` or the `gym-postgres` Docker container running. It builds a throwaway `grafana_weekly_test` schema, applies `sql/grafana_weekly_views.sql` against it, asserts, and drops the schema. Override the connection with `POSTGRES_URL=...`.

## Architecture notes that span multiple files

**The schema lives in two places and must stay in sync.** `fetch_counts.py:connect()` defines the SQLite `occupancy` table; `sync_to_postgres.py:SCHEMA_SQL` defines the Postgres equivalent plus a `(fetched_at, venue)` unique index used for `ON CONFLICT` upsert. Adding a column means editing both, plus the column tuple in `OCCUPANCY_COLUMNS`, the `COPY` column list, and the `read_sqlite_rows` SELECT.

**Sync uses staging + upsert, not direct insert.** `sync_to_postgres.py` shells out to `psql` three times — create-tables/truncate-staging, `COPY ... FROM STDIN` the full SQLite contents into `occupancy_import`, then `INSERT ... ON CONFLICT DO UPDATE` from staging into `occupancy`. This makes re-syncs idempotent and avoids a Python Postgres driver dependency.

**Opening-hours logic is duplicated in three places.** Python-side in `fetch_counts.py:is_open_time()` (Mon-Fri 06-22, Sat 09-22, Sun 09-18); SQL-side in `sql/grafana_weekly_views.sql` (the `open_slots` CTE); and JS-side in the heatmap panel of `grafana/weekly-dashboard.json` (panel id 2's `OPEN_HOURS_MIN` table, used to label cells outside hours as "closed"). All three must stay in sync. The SQL side localizes UTC `fetched_at` via `AT TIME ZONE 'Asia/Taipei'` before bucketing.

**Grafana dashboard is wired to a stable datasource UID `gym-postgres`.** `grafana/weekly-dashboard.json` references this UID and `grafana/provisioning/datasources/datasources.yml` creates it at Grafana startup. The dashboard JSON is the **bare dashboard model** (no `{"dashboard": {...}, "overwrite": true}` wrapper) because file provisioning expects that shape; do not re-wrap it. The dashboard requires the `volkovlabs-echarts-panel` plugin (auto-installed via `GF_INSTALL_PLUGINS` in `docker-compose.yml`) and exposes two extra template variables, `${granularity}` (`30min`/`60min`, default `60min`) and `${palette}` (`terracotta`/`sage`/`slate`/`rose`/`teal`/`lavender`, default `slate`), that are read inside the ECharts panel JS to switch SQL bucketing and color stops at render time.

**Grafana picks up dashboard edits from disk.** `grafana/provisioning/dashboards/providers.yml` watches `/var/lib/grafana/dashboards` (bind-mounted from `grafana/weekly-dashboard.json`) every 10 s. Save the JSON, wait ten seconds, reload the browser — no curl POST. `allowUiUpdates: false` keeps the UI editor read-only; flip to `true` and `docker compose restart grafana` if you want the UI to be editable again.

**The dashboard has four panels stacked top-to-bottom:** (1) `Popular Times` bar chart driven by `weekly_occupancy_slots` filtered to today's `ISODOW`, with the current hour highlighted via `palette[5]`; (4) `Live Cards` two-up with sparkline of today's `occupancy` readings and a delta-vs-historical-median row driven by `current_vs_history`; (2) `Weekly 30-Minute Occupancy Heatmap`; (3) `Selected Venue Exact Counts` line chart. Panel 1 and 4 both read `${venue}` and `${palette}` only — no new template variables. Panel titles and all in-panel labels are English; venue names stay Chinese (source data). The public static page (`tools/render_public_html.py`) maps venue names to English display names — see `docs/SERVER_SETUP.md`.

**SSL workaround is intentional, not a bug.** `fetch_counts.py:make_ssl_context()` clears `VERIFY_X509_STRICT` because the NTU institutional cert chain omits Subject Key Identifier extensions that OpenSSL 3.x strict mode rejects. Don't replace it with an unverified context — full chain + hostname verification is still on.

**`fetch_counts.py` redirects `sys.stdout`/`sys.stderr` to a `DailyLogStream`** unless `--no-log-file` is set. This writes to `logs/<YYYY-MM-DD>.log` in addition to the terminal, so anything you print from inside long-running code shows up there. `logs/` is git-ignored.

## Conventions worth knowing

- Timestamps stored as ISO strings with timezone (`datetime.now().astimezone().replace(second=0, microsecond=0).isoformat()`). Rows are aligned to the minute, not the second.
- SQLite `.bak-*` files in the repo root are produced by `rsync.sh` — they're snapshots of the previous local DB before an rsync overwrote it. Safe to delete; git-ignored.
- Don't commit `*.sqlite3`, `*.csv`, or anything under `logs/` — all ignored.
- Plans/specs from the `superpowers` workflow live under `docs/superpowers/` and the brainstorming scratch under `.superpowers/brainstorm/` (ignored).
