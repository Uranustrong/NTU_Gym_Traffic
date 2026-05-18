# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

**Opening-hours logic is duplicated.** Python-side in `fetch_counts.py:is_open_time()` (Mon-Fri 08-22, Sat 09-22, Sun 09-18) and SQL-side in `sql/grafana_weekly_views.sql` (the `open_slots` CTE). Changes must be applied to both, and remember the SQL side localizes UTC `fetched_at` via `AT TIME ZONE 'Asia/Taipei'` before bucketing.

**Grafana dashboard is wired to a hardcoded datasource UID.** `grafana/weekly-dashboard.json` references `bfm1ctqr9jgn4b`. If the local datasource has a different UID, the curl-based import will produce panels that can't query. The dashboard also requires the `volkovlabs-echarts-panel` plugin (for the heatmap and timeseries panels — see `README_Local.md`) and exposes two extra template variables, `${granularity}` (`30min`/`60min`, default `60min`) and `${palette}` (`terracotta`/`sage`/`slate`/`rose`/`teal`/`lavender`, default `slate`), that are read inside the ECharts panel JS to switch SQL bucketing and color stops at render time.

**SSL workaround is intentional, not a bug.** `fetch_counts.py:make_ssl_context()` clears `VERIFY_X509_STRICT` because the NTU institutional cert chain omits Subject Key Identifier extensions that OpenSSL 3.x strict mode rejects. Don't replace it with an unverified context — full chain + hostname verification is still on.

**`fetch_counts.py` redirects `sys.stdout`/`sys.stderr` to a `DailyLogStream`** unless `--no-log-file` is set. This writes to `logs/<YYYY-MM-DD>.log` in addition to the terminal, so anything you print from inside long-running code shows up there. `logs/` is git-ignored.

## Conventions worth knowing

- Timestamps stored as ISO strings with timezone (`datetime.now().astimezone().replace(second=0, microsecond=0).isoformat()`). Rows are aligned to the minute, not the second.
- SQLite `.bak-*` files in the repo root are produced by `rsync.sh` — they're snapshots of the previous local DB before an rsync overwrote it. Safe to delete; git-ignored.
- Don't commit `*.sqlite3`, `*.csv`, or anything under `logs/` — all ignored.
- Plans/specs from the `superpowers` workflow live under `docs/superpowers/` and the brainstorming scratch under `.superpowers/brainstorm/` (ignored).
