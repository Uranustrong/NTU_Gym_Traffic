# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 輸出語言

預設使用**繁體中文（台灣）**作為主要輸出語言，技術名詞、程式碼、檔案路徑、commit message、代碼註解仍維持英文。用戶習慣中英混用，回覆風格保持精簡、技術導向。

## Project shape

Scrapes NTU sports center occupancy from `rent.pe.ntu.edu.tw` every 5 minutes during opening hours, stores rows in a local SQLite DB, and (optionally) syncs them into a Dockerized PostgreSQL for Grafana to visualize.

Pure Python standard library — no third-party packages. `requirements.txt` is intentionally empty.

## Deployment: mid-migration, ws7 is gone

The ws7 collector no longer exists. It ran out of `/tmp2/b12902066/Gym_Fetch`, machine-local scratch space that was wiped around **2026-08-08 03:29**, taking the Postgres container and roughly 48% of the collected history with it. Verified afterwards: no crontab on ws3 or ws7, no `/var/spool/cron/crontabs/`, nothing left in `$HOME`. Only the static public page at `~/htdocs/gym/index.html` survived, because it lives in the NFS home rather than in scratch.

The project is being moved to **GitHub Actions + Supabase**; the approved plan is [`docs/plan/2026-08-09-github-actions-supabase-migration.md`](docs/plan/2026-08-09-github-actions-supabase-migration.md).

**Phase A is only partly done — do not cut over.** The parts that needed no cloud account are complete: A0 recovery, A1 sync refactor, A5 fetch hardening, A8 migrations. Three pre-cutover blockers are still open because they all require the Supabase project to exist first: **A4** (data-only backup plus a rehearsed restore), **A6** (measuring whether `COPY ... FROM STDIN` survives the transaction pooler on :6543), **A7** (repointing the local Grafana datasource at `gym_reader` over TLS). Phase B — Supabase project, secrets, workflows, GitHub Pages — has not started. There is no collector running anywhere right now.

`docs/recovery/` holds the archived page and the 2,444 rows rebuilt from it. Reconstructed rows are identifiable by `source_updated_at IS NULL` — every row written by the real collector has that column populated.

**Stale docs, do not trust yet:** `README_Server.md` and `README_Local.md` still describe the ws7 + rsync setup and still claim weekday opening is 08:00; `docs/SERVER_SETUP.md` says the UserDir root is `~/public_html` when CSIE actually serves `~/htdocs`. `rsync.sh`, `tools/sync_local.sh`, `tools/psql_gym_postgres.sh` and `crontab.example` are all vestiges of that deployment. Rewriting them is follow-up item D3 in the plan.

## Common commands

```bash
# One-shot fetch into gym_counts.sqlite3 (no logging to file)
python3 fetch_counts.py --no-log-file

# What the collector workflow runs: refuse a partial page or a frozen source
python3 fetch_counts.py --no-log-file --open-hours-only \
  --expect-venues 健身中心,室內游泳池

# Long-running, aligned-to-5min, skips closed hours
python3 fetch_counts.py --loop --open-hours-only

# Create/refresh schema, views, roles and RLS (owner credential)
export PGPASSWORD='...' GYM_WRITER_PASSWORD='...' GYM_READER_PASSWORD='...'
tools/apply_migrations.sh postgresql://songhejun@127.0.0.1:5433/gym_fetch
tools/apply_migrations.sh --skip-roles postgresql://...   # schema + views only

# Sync SQLite → Postgres. Leave the password out of the URL; libpq reads
# PGPASSWORD from the environment, which keeps it out of `ps`.
PGPASSWORD='...' python3 sync_to_postgres.py \
  --sqlite-db gym_counts.sqlite3 \
  --postgres-url postgresql://songhejun@127.0.0.1:5433/gym_fetch

# Backfill or repair existing rows (needs an owner credential)
PGPASSWORD='...' python3 sync_to_postgres.py --on-conflict update \
  --sqlite-db gym_counts.sqlite3 --postgres-url postgresql://...

# Same, but when host has no psql — wrap through Docker
python3 sync_to_postgres.py \
  --sqlite-db gym_counts.sqlite3 \
  --postgres-url postgresql://songhejun@127.0.0.1:5433/gym_fetch \
  --psql tools/psql_gym_postgres.sh

# Bring up the whole stack (Grafana + Postgres) on a clean machine
cp .env.example .env  # then edit passwords
docker compose up -d

# Rebuild the recovered 2026-08-01..08-07 rows from the archived public page
python3 tools/recover_from_public_page.py \
  --page docs/recovery/gym-page-2026-08-08.html --sqlite gym_counts.sqlite3

# Tests. The DB integration tests skip unless explicitly armed, so this is
# always safe to run.
python3 -m unittest discover -p 'test_*.py'

# Arming them. TEST_POSTGRES_URL is separate from POSTGRES_URL on purpose:
# these tests DROP schemas and roles, and an exported POSTGRES_URL for real
# work must never be enough to point them at a live database.
TEST_POSTGRES_URL=postgresql://songhejun@127.0.0.1:5433/gym_fetch \
ALLOW_DESTRUCTIVE_DB_TESTS=1 python3 -m unittest discover -p 'test_*.py'
```

Two of the five test files are **destructive integration tests**. `dbtest_support.py` gates them: it needs `TEST_POSTGRES_URL` *and* `ALLOW_DESTRUCTIVE_DB_TESTS=1`, refuses managed hosts (`*.supabase.co`, `*.rds.amazonaws.com`, …) unless `ALLOW_DESTRUCTIVE_DB_TESTS_ON_REMOTE=1` is also set, and suffixes every schema and role name per process so nothing can collide with a real object. They skip rather than fail when unarmed:

- `test_sync_to_postgres_integration.py` — proves what mocks cannot: that PostgreSQL accepts the generated script, that the inline `COPY` payload survives psql's own line splitting, and that a failed verification rolls the **whole** transaction back. Works inside a throwaway schema selected via libpq's `options=-csearch_path=`.
- `test_grafana_weekly_views.py` — builds a throwaway schema, applies `sql/grafana_weekly_views.sql`, asserts, drops it.

## Architecture notes that span multiple files

**The schema lives in two places and must stay in sync.** `fetch_counts.py:connect()` defines the SQLite `occupancy` table; `sql/migrations/001_occupancy.sql` defines the Postgres equivalent plus the `(fetched_at, venue)` unique index that drives `ON CONFLICT`. Adding a column means editing both, plus the `OCCUPANCY_COLUMNS` tuple in `sync_to_postgres.py` — that one tuple now generates the `COPY` list, the `INSERT` list and the `read_sqlite_rows` SELECT, so there is nothing else to update.

**Postgres state is version-controlled, not clicked into a SQL editor.** `sql/migrations/*.sql` is applied in filename order by `tools/apply_migrations.sh`: `001` the table, `002` the views (a symlink to `sql/grafana_weekly_views.sql`, which keeps its own path because tests, docs and `docker-compose.yml` all reference it), `003` the `gym_writer`/`gym_reader` roles, `004` RLS and policies. Every file is idempotent. `docker-compose.yml` mounts `001` and `002` into `docker-entrypoint-initdb.d` — roles and RLS are skipped there because the initdb hook cannot supply the password psql variables.

**Sync is one psql session, one transaction, and verifies itself.** `sync_to_postgres.py` builds a single script — `BEGIN`, `CREATE TEMP TABLE occupancy_import ... ON COMMIT DROP`, an inline `COPY ... FROM STDIN` payload terminated by `\.`, `INSERT ... ON CONFLICT`, a `DO $$` block that fails the transaction unless every staged row is present with matching values, `COMMIT` — and feeds it to `psql -f -`. Four consequences worth knowing:

- Staging is session-local, so concurrent runs cannot truncate each other's rows. The previous design used a shared `public.occupancy_import` across three connections; interleaved runs silently dropped a reading and still printed success.
- Retries are safe. A commit whose response was lost re-runs, hits the conflict clause, writes nothing, and still passes verification.
- No DDL is needed on `public`, which is what lets the collector run as a SELECT+INSERT-only role.
- `rows_to_csv()` must emit `\n`, not the csv module's default `\r\n`: psql splits the embedded payload into lines itself, and a surviving CR corrupts the last column.

Default conflict strategy is `DO NOTHING` (collection only appends). `--on-conflict update` switches to `DO UPDATE` for backfills and repairs, and needs an owner credential because `gym_writer` has no UPDATE privilege and RLS confines its inserts to roughly the last hour.

**Opening-hours logic is duplicated in four places.** Python-side in `fetch_counts.py:is_open_time()` (Mon-Fri 06-22, Sat 09-22, Sun 09-18); SQL-side in `sql/grafana_weekly_views.sql` (the `open_slots` CTE, using ISODOW 1-7); JS-side in the heatmap panel of `grafana/weekly-dashboard.json` (panel id 2's `OPEN_HOURS_MIN` table, used to label cells outside hours as "closed"); and again in SQL in `tools/render_public_html.py:sql_panel3()`. All four must stay in sync. The SQL sides localize `fetched_at` via `AT TIME ZONE 'Asia/Taipei'` before bucketing.

**Weekday opening really is 06:00, not the 08:00 the website advertises.** `rent.pe.ntu.edu.tw` lists 08:00-22:00 for the 綜合體育館 *building*, but 健身中心 is populated from 06:00 on every weekday in the collected history (06:00 typically 6-17 people, 07:00 typically 19-34). Commit `28a4ae6` corrected this on purpose; "fixing" it back to 08:00 discards two hours of real data every weekday.

**Timezone is pinned, never inherited.** `fetch_counts.py` uses `now_taipei()` (`zoneinfo.ZoneInfo("Asia/Taipei")`), not `datetime.now().astimezone()`. `is_open_time()` compares bare hour numbers, so on a UTC host — every GitHub Actions runner — inheriting the host clock would make `--open-hours-only` skip nearly every scheduled run without erroring. Stored rows carry an explicit `+08:00` offset and the Postgres column is `TIMESTAMPTZ`, so this affects decisions, not stored history.

**Grafana dashboard is wired to a stable datasource UID `gym-postgres`.** `grafana/weekly-dashboard.json` references this UID and `grafana/provisioning/datasources/datasources.yml` creates it at Grafana startup. The dashboard JSON is the **bare dashboard model** (no `{"dashboard": {...}, "overwrite": true}` wrapper) because file provisioning expects that shape; do not re-wrap it. The dashboard requires the `volkovlabs-echarts-panel` plugin (auto-installed via `GF_INSTALL_PLUGINS` in `docker-compose.yml`) and exposes two extra template variables, `${granularity}` (`30min`/`60min`, default `60min`) and `${palette}` (`terracotta`/`sage`/`slate`/`rose`/`teal`/`lavender`, default `slate`), that are read inside the ECharts panel JS to switch SQL bucketing and color stops at render time.

**Grafana picks up dashboard edits from disk.** `grafana/provisioning/dashboards/providers.yml` watches `/var/lib/grafana/dashboards` (bind-mounted from `grafana/weekly-dashboard.json`) every 10 s. Save the JSON, wait ten seconds, reload the browser — no curl POST. `allowUiUpdates: false` keeps the UI editor read-only; flip to `true` and `docker compose restart grafana` if you want the UI to be editable again.

**The dashboard has four panels stacked top-to-bottom:** (1) `Popular Times` bar chart driven by `weekly_occupancy_slots` filtered to today's `ISODOW`, with the current hour highlighted via `palette[5]`; (4) `Live Cards` two-up with sparkline of today's `occupancy` readings and a delta-vs-historical-median row driven by `current_vs_history`; (2) `Weekly 30-Minute Occupancy Heatmap`; (3) `Selected Venue Exact Counts` line chart. Panel 1 and 4 both read `${venue}` and `${palette}` only — no new template variables. Panel titles and all in-panel labels are English; venue names stay Chinese (source data). The public static page (`tools/render_public_html.py`) maps venue names to English display names — see `docs/SERVER_SETUP.md`.

**SSL workaround is intentional, not a bug.** `fetch_counts.py:make_ssl_context()` clears `VERIFY_X509_STRICT` because the NTU institutional cert chain omits Subject Key Identifier extensions that OpenSSL 3.x strict mode rejects. Don't replace it with an unverified context — full chain + hostname verification is still on.

**`fetch_counts.py` redirects `sys.stdout`/`sys.stderr` to a `DailyLogStream`** unless `--no-log-file` is set. This writes to `logs/<YYYY-MM-DD>.log` in addition to the terminal, so anything you print from inside long-running code shows up there. `logs/` is git-ignored.

## Conventions worth knowing

- Timestamps stored as ISO strings with timezone (`datetime.now().astimezone().replace(second=0, microsecond=0).isoformat()`). Rows are aligned to the minute, not the second.
- SQLite `.bak-*` files in the repo root are produced by `rsync.sh` — they're snapshots of the previous local DB before an rsync overwrote it. Safe to delete; git-ignored.
- Don't commit `*.sqlite3`, `*.csv`, or anything under `logs/` — all ignored.
- Cron jobs run through `tools/cronlog.sh`, which writes per-day logs to `logs/<YYYY-MM>/<YYYY-MM-DD>-<name>.log` (`<name>` = `cron`/`sync`/`render`). The cron fetch passes `--no-log-file` so `fetch_counts.py`'s own `DailyLogStream` (`logs/<YYYY-MM-DD>.log`) is left for interactive runs only.
- Plans/specs from the `superpowers` workflow live under `docs/superpowers/` and the brainstorming scratch under `.superpowers/brainstorm/` (ignored).
- Deferred features and undecided design calls are tracked in `docs/TODO.md` (the feature backlog) — consult it before starting feature work, and move items out when they ship.
