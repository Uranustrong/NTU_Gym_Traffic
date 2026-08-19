# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 輸出語言

預設使用**繁體中文（台灣）**作為主要輸出語言，技術名詞、程式碼、檔案路徑、commit message、代碼註解仍維持英文。用戶習慣中英混用，回覆風格保持精簡、技術導向。

## Project shape

Scrapes NTU sports center occupancy from `rent.pe.ntu.edu.tw` every 5 minutes during opening hours, stores rows in a local SQLite DB, and (optionally) syncs them into a Dockerized PostgreSQL for Grafana to visualize.

Pure Python standard library — no third-party packages. `requirements.txt` is intentionally empty.

## Deployment: GitHub Actions + Supabase

The ws7 collector no longer exists. It ran out of `/tmp2/b12902066/Gym_Fetch`, machine-local scratch space that was wiped around **2026-08-08 03:29**, taking the Postgres container and roughly 48% of the collected history with it. Verified afterwards: no crontab on ws3 or ws7, no `/var/spool/cron/crontabs/`, nothing left in `$HOME`. Only the static public page at `~/htdocs/gym/index.html` survived, because it lives in the NFS home rather than in scratch.

**Phases A and B are complete and running**, and follow-up D4 (the page reading Supabase directly) shipped with them. Two plans, each carrying a status banner of what actually happened versus what was designed: [`2026-08-09-github-actions-supabase-migration.md`](docs/plan/2026-08-09-github-actions-supabase-migration.md) and [`2026-08-09-d4-public-page-reads-supabase.md`](docs/plan/2026-08-09-d4-public-page-reads-supabase.md). Phase C (a week of delivery measurement) and the rest of Phase D are still open.

Three workflows, all verified end to end on 2026-08-09:

| Workflow | When | Woken by | Role | Port |
|---|---|---|---|---|
| `collect.yml` | every 5 min at `:00,:05…:55`, opening hours only | Supabase `pg_cron`, GitHub schedule as fallback | `gym_writer` | 6543 |
| `publish.yml` | on pushes to `main` that touch the page's code | GitHub `push` / manual | **none** | — |
| `backup.yml` | Mondays 04:20 | GitHub schedule only | `gym_reader` | **5432** |

The public page is live at **https://uranustrong.github.io/NTU_Gym_Traffic/** (Pages source is *GitHub Actions*, not a branch).

**It is a static shell that fetches its own data.** The browser calls `public.gym_heatmap()` and `public.gym_live()` on Supabase, so the page is deployed once and stays live on its own — `publish.yml` is a code deploy, not a data pipeline, and holds no database credentials at all. See the API section below.

- **Supabase is live** (PostgreSQL 17.6) with all five migrations applied. 15,464 rows were backfilled — 13,020 original plus 2,444 recovered — and collection has been appending since. **Database size is not the constraint that will bite**: `occupancy` plus its indexes is ~2.9 MB of the 500 MB ceiling and grows about 24 MB a year, which is roughly eighteen years of headroom. Egress (5 GB/month) and the shared CPU are the limits worth watching, which is what the rate limiting below is for.
- **Roles behave as designed, verified against the real project**: `gym_writer` can insert current readings, is blocked by RLS from backdating, and is denied UPDATE and DELETE; `gym_reader` is read-only; `anon` and `authenticated` hold no privileges on the table or either view, and `anon`'s only reach into the database at all is EXECUTE on the two wrapper functions of 005.
- **Port 6543 (transaction pooler) for collect**, settled by measurement: `COPY ... FROM STDIN` plus a TEMP staging table works there because the whole sync is one transaction. **`backup.yml` must use 5432** — `pg_dump` holds one snapshot across many statements, which transaction pooling does not preserve. That is why the port is hardcoded there instead of reading `PG_PORT`.
- **GitHub secrets and variables are set.** Secrets: `SUPABASE_{WRITER,READER}_{URL,PASSWORD}` — URLs carry no password, no port, no sslmode, so `PGPORT`/`PGSSLMODE` from the environment are what actually apply. Variables: `PG_PORT=6543`, `PG_SSLMODE=require`, `EXPECT_VENUES`, `PUBLISH_DAYS=7`, plus `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` for the page. The last two are variables rather than secrets on purpose: they are printed into the public page by design, and masking them in logs would only make failures harder to read. **The key is validated, not just length-checked** — `check_publishable_key()` in `tools/render_public_html.py` refuses `sb_secret_...` and any JWT whose `role` claim is not `anon`, and `publish.yml` additionally greps the built page for those markers. Length alone cannot tell a publishable key from a service_role one, and a key published to GitHub Pages can only be rotated, never recalled.

Local credentials live in `supabase.secrets` (gitignored, see `supabase.secrets.example`). It is meant to be **sourced, never read**:

```bash
bash -c 'set -a; . ./supabase.secrets; set +a; psql "$SUPABASE_OWNER_URL" -X -c "SELECT count(*) FROM occupancy;"'
```

The owner credential is deliberately absent from GitHub. Migrations, backfills and anything needing `ON CONFLICT DO UPDATE` run from the Mac.

**Next: Phase C** — after a week of collection, run the four measurement queries in section C2 of the plan and decide whether to stay at 5-minute cadence or drop to 10.

`docs/recovery/` holds the archived page and the 2,444 rows rebuilt from it. Reconstructed rows are identifiable by `source_updated_at IS NULL` — every row written by the real collector has that column populated.

**That rescue is not repeatable.** It worked only because the old page baked raw readings into its HTML; the page is now a shell and a future archived copy would contain no data at all. What replaces it is deliberate rather than accidental: `backup.yml` dumps `public.occupancy` weekly, proves the dump by restoring it into a database built from `sql/migrations/` alone, keeps the artifact 90 days, and refreshes a permanent `data-latest` release asset.

**Still pointing at the old world:** ws7's `~/htdocs/gym/index.html` is still serving the frozen 2026-08-08 page and still claims to auto-refresh; it should become a redirect to the Pages URL (plan item B6). `README_Server.md` and `README_Local.md` still describe the ws7 + rsync setup and still claim weekday opening is 08:00. `rsync.sh`, `tools/sync_local.sh`, `tools/psql_gym_postgres.sh`, `tools/cronlog.sh` and `crontab.example` are all vestiges of that deployment. Rewriting them is follow-up item D3.

## Common commands

```bash
# Workflows. `collect --force` fetches outside opening hours and skips the
# source-freshness check, which is the only way to smoke-test after closing.
gh workflow run collect.yml                 # a real collection, now
gh workflow run collect.yml -f force=true   # smoke test outside opening hours
gh workflow run publish.yml                 # redeploy the page shell (data is fetched live)
gh workflow run backup.yml                  # dump, restore-verify, release, keepalive
gh run list --workflow=collect.yml --limit 10
gh run view <run-id> --log

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

Three of the seven test files are **destructive integration tests**. `dbtest_support.py` gates them: it needs `TEST_POSTGRES_URL` *and* `ALLOW_DESTRUCTIVE_DB_TESTS=1`, refuses managed hosts (`*.supabase.co`, `*.rds.amazonaws.com`, …) unless `ALLOW_DESTRUCTIVE_DB_TESTS_ON_REMOTE=1` is also set, and suffixes every schema and role name per process so nothing can collide with a real object. They skip rather than fail when unarmed:

- `test_sync_to_postgres_integration.py` — proves what mocks cannot: that PostgreSQL accepts the generated script, that the inline `COPY` payload survives psql's own line splitting, and that a failed verification rolls the **whole** transaction back. Works inside a throwaway schema selected via libpq's `options=-csearch_path=`.
- `test_grafana_weekly_views.py` — builds a throwaway schema, applies `sql/grafana_weekly_views.sql`, asserts, drops it.
- `test_public_api_migration.py` — applies `001`, `002` and `005` **twice** and asserts the closure API's boundary matrix, `gym_live`'s payload shape and seed idempotency. It is the one that needs a throwaway **database** rather than a schema, because those files write into `public` by name. **It deliberately skips `003`**: that file's `ALTER ROLE gym_writer PASSWORD` is cluster-wide, so running it against a developer's own Postgres would reset the passwords of their real collector roles. The cost is that the revoke matrix is not covered here — that has to be checked against the real `anon` and a live PostgREST anyway, and with a positive control, because a keyless request also returns 401.

`test_public_page_js.py` is the seventh and skips on a different axis: it runs the page's `closureSpan()` and panel 3's `closureBands()` under `node` and skips when node is absent, rather than needing a database. It reads from two files — the template for one, `grafana/weekly-dashboard.json` for the other — because that is where each piece actually lives.

## Architecture notes that span multiple files

**The public page's data comes from two functions, and `anon` can reach nothing else.** `sql/migrations/005_public_api.sql` defines two layers. `api_private.gym_panel*_rows()` are the canonical relational primitives — the single definition of each panel's SQL, which the Grafana dashboard's `rawSql` also selects from. `public.gym_heatmap()` and `public.gym_live()` are `SECURITY DEFINER` JSON wrappers that assemble Grafana's frame shape, and they are the only things PostgREST can see. Points that are easy to get wrong:

- **`api_private` is not exposed by PostgREST**, so `anon` gets a 404 on the primitives rather than a 403 — invisible beats forbidden, and a future stray `GRANT` there would still be unreachable. Verified: `anon` and `authenticated` hold EXECUTE on the two wrappers and nothing else, and neither has `USAGE` on `api_private`.
- **`anon` needs no privilege on `occupancy` and no RLS policy of its own**, because the wrappers run as their owner. 004 was untouched by 005 and stayed that way until the closures work of 2026-08-19, which added the new objects to its revoke list and gave `closures` an RLS policy for `gym_reader`.
- **`gym_reader` needs EXECUTE on the primitives**, granted here rather than in 003, because Grafana's Supabase datasource connects as that role and its panels now call them.
- **The browser sends `apikey` only, never `Authorization: Bearer`.** A `sb_publishable_...` key is not a JWT and is rejected as a malformed one; that header is for a signed-in user's token and there is no sign-in here.
- **The two wrappers are unauthenticated compute, and the thing they can exhaust is quota, not secrecy.** The data is public by design. What is not free is the Free plan: 5 GB egress a month, 500 MB of database, and a *shared* CPU that collection also needs. Measured before any mitigation, one single-threaded `curl` loop sustained 1.9 GB/day against `gym_live` — the monthly egress in **2.6 days** — and at 510 ms of work per 700 ms round trip it alone occupied ~73% of a core while the collector's `COPY` (175 ms) competed for the rest. Egress exhaustion at least sends mail and a grace period; CPU starvation just silently drops readings, and a missed five-minute slot is gone for good.
- **Edge caching is not available, so it is all done in-database.** Supabase's Cloudflare returns `cf-cache-status: DYNAMIC` for `/rest/v1/rpc/*` even when the function emits `Cache-Control: public, max-age=3600` — verified against this project by publishing a probe function and reading the response headers. The header does reach the browser; nothing caches it in between.
- **`gym_heatmap` is computed on a timer, not per request: 510 ms → 2.0 ms**, measured through PostgREST. Its cost was entirely the full-history aggregate behind `weekly_occupancy_slots`, which changes meaningfully about once a day, so `api_private.gym_heatmap_cache` stores the assembled body and `pg_cron` rebuilds it hourly. The wrapper reads one row and adds the freshness header live. Three things keep that from being fragile: a stale, missing **or empty** payload is rebuilt by the next caller, so `pg_cron` is an optimisation rather than a dependency (which is what lets 005 still apply to the bare `postgres:17` of the restore drill); the rebuild is guarded by `pg_try_advisory_xact_lock` so a cold-cache stampede costs one rebuild, not N; and losing that lock serves the slightly stale copy, which for a multi-month average is the right answer.
- **Three rate limits, and the middle one exists because of NTU.** `api_private.rate_limit_check()` runs at the top of both wrappers. Per client per minute (60 / 30) stops a runaway loop within seconds; **campus wifi is NAT**, so this cannot be sized for one person — 60/min is about 300 tabs behind one address. Per client per day (3000 / 1500) stops one address spending the whole day's allowance before lunch. Global per day (8000 / 4000 ≈ 2.8 GB/month) is what actually bounds the bill. Every number lives in `api_private.rate_limit_policy` and is tunable with an `UPDATE`; the migration's seed uses `ON CONFLICT DO NOTHING`, so hand-tuning survives re-running it. Points worth knowing:
  - **Refusal is `RAISE SQLSTATE 'PT429'`**, which PostgREST turns into a real HTTP 429 (verified end to end, not assumed). Raising also rolls the increment back, so a blocked caller pins the counter at the limit instead of running it away, and a refused call spends no global budget because it produced no egress either.
  - **Only HTTP callers are limited.** The client key comes from `cf-connecting-ip` in `request.headers`, which PostgREST sets and a direct libpq connection does not have — so migrations, `psql`, `pg_cron` and Grafana are exempt without an allowlist, and none of them is anonymous anyway.
  - **Both counter tables are `UNLOGGED`.** This is counters, not records: it keeps a write-per-request out of the WAL, and a restart clearing them fails open, which is the right direction for a limiter guarding a hobby project.
  - **Both wrappers had to become `VOLATILE`** — a `STABLE` function may not write, and counting is writing. Harmless here: the page has always used POST (only PostgREST's GET form requires `STABLE`), and the bodies are fixed SQL running as their owner with `search_path` pinned to `''`.
  - **The residual risk is stated, not hidden:** a caller spread across many addresses can still spend the global budget, and the page then degrades to its cached data until 00:00 UTC. Collection, Grafana and backups are untouched.
- **The kill switch is still one statement**, if it ever comes to that — it degrades only the page:

  ```sql
  REVOKE EXECUTE ON FUNCTION public.gym_heatmap(), public.gym_live(integer) FROM anon;
  ```

- **`anon` holds EXECUTE on `net.http_post` and `cron.schedule`, and that is Supabase's own default, not ours.** It looks alarming in a privilege audit and is unreachable: PostgREST exposes `public` only (`/rest/v1/rpc/http_post` is a 404), and `anon` is `NOLOGIN`, so there is no path to it. Do not "fix" it by revoking Supabase's stock grants.

- **Staleness is judged from `dataAsOf`, never from "the request succeeded".** A stopped collector still answers 200 — viewers polling the API are themselves enough to keep a Free project awake — so a page trusting the response would quietly re-cache old numbers as new. `gym_live` therefore returns `dataAsOf`, `servedAt` and `freshnessExpected` (whether the centre is open now, decided in the database so the page needs no timezone logic of its own). Only `gym_live` may set freshness: `gym_heatmap` is cached for six hours by design.

**Supabase wakes the collector; GitHub's own schedule is only the fallback.** GitHub delivered roughly one scheduled run every 22 minutes against a nominal 5, alternating between `collect` and `publish` as though the cap were per repository, and the interval was not converging after an hour. Manual dispatches, by contrast, started instantly every time — five for five. So `sql/supabase/001_dispatch_cron.sql` puts four `pg_cron` jobs on Supabase that call `ops.dispatch_workflow('collect.yml')`, which `pg_net` turns into a `workflow_dispatch` POST. Nothing about the collector changed; only what wakes it. (It once scheduled `publish.yml` too; that file now unschedules those jobs, because the page no longer needs rebuilding to be fresh.)

Three consequences to keep in mind:

- **Nothing under `sql/supabase/` is a migration.** `tools/apply_migrations.sh` globs `sql/migrations/*.sql`, and the weekly restore drill applies those to a bare `postgres:17` container that has neither `pg_cron` nor `pg_net`. Putting either file there would break the backup verification. Both are applied by hand and both are idempotent — re-running `001` rotates the token, and `002_cost_control_cron.sql` (hourly heatmap-cache rebuild, hourly rate-limiter sweep) upserts its jobs by name. `002` is deliberately optional: 005 rebuilds a stale cache on demand and the limiter sweeps itself probabilistically, so a database without these jobs is slower, never wrong.
- **The PAT expires 2026-11-07.** When it does, `pg_net` gets a 401 and nothing happens — no error surfaces anywhere. `select * from ops.dispatch_log;` decodes the status code (204 ok, 401 expired, 403 permissions narrowed, 404 cannot see the repo). This is exactly why `collect.yml` keeps its `schedule` block: expiry degrades collection to GitHub's ~20%, not to zero.
- **pg_cron schedules are UTC** and cannot be given a timezone, so `sql/supabase/001_dispatch_cron.sql` carries a hand-shifted copy of the opening-hours windows. Taipei has no DST so the −8 offset is fixed, but this is now a **fifth** place the opening hours are written down.

**A newly added `schedule` takes about an hour before GitHub first honours it.** Measured, not guessed: `collect.yml`'s cron landed on `main` at 06:10 UTC on 2026-08-09 and first fired at 07:12:58 — 62 minutes and twelve missed slots later, with nothing wrong anywhere. There is no API that reports whether a schedule has been registered, and `workflow_dispatch` succeeding proves nothing about it because it takes a different path entirely. So do not diagnose a silent schedule for at least an hour, and do not rewrite crons in the meantime. The same run also settled that the `timezone:` key works: it fired at Taipei 15:13, matching `3-59/5 9-17 * * 0` only under the Taipei reading — 07:13 UTC matches none of the three windows.

**The weekly empty commit is load-bearing, not noise.** GitHub disables a public repo's scheduled workflows after 60 days with no *repository* activity, and workflow runs do not count — 192 collections a day would not have kept it alive. `backup.yml`'s last step pushes `chore: keepalive <timestamp>` as `github-actions[bot]` and then calls the `/enable` endpoint for `collect.yml` as a second line of defence. That push does not trigger workflows (GitHub suppresses recursion for `GITHUB_TOKEN`), but it does reset the clock. If branch protection is ever turned on for `main`, the push is rejected and the step fails loudly on purpose; silently letting it fail would kill collection 60 days later with no other warning.

**The SQLite file on a runner is scratch, not storage.** Each `collect.yml` run starts with an empty database, writes two rows, syncs them, and is destroyed. That is why `read_sqlite_rows()` having no WHERE clause costs nothing there — but also why a failed sync loses the reading permanently, hence three retries and an `if: failure()` artifact. The row count, never the file's existence, decides whether there is anything to sync: `connect()` runs before the opening-hours check, so a closed-hours run still leaves a valid empty database behind.

**The source goes away for minutes at a time, so retrying is bounded by the clock, not by a count.** Measured on 2026-08-13 across 400 runs: five failed, each costing exactly one 5-minute slot, and four of the five were `rent.pe.ntu.edu.tw` answering **200 with no `CMCItem` markup in the body** — not a network error, which would have printed `fetch failed:` instead. The decisive measurement was on the successes: of 80 consecutive successful runs, **zero** were rescued by a retry. Independent per-request failures at a 1% run-failure rate would need a per-request rate near 20%, which would have shown roughly a third of those 80 recovering visibly. None did. The source is either fine on the first try or gone for longer than the entire ladder, so `DEFAULT_RETRIES=2` × `DEFAULT_RETRY_DELAY_SECONDS=10` was rescuing nothing at all. `--fetch-budget-seconds` replaces the count with a wall-clock deadline (150s in `collect.yml`, backoff capped by `MAX_RETRY_DELAY_SECONDS`); omitting the flag keeps the old fixed ladder, which is what interactive runs and the `force=true` smoke test want. Two things that are easy to get wrong here:

- **Freshness is judged against `now_taipei()`, not against `fetched_at`.** `main()` pins `fetched_at` to the slot start, and a batch rescued three minutes into a long retry carries a source timestamp three minutes *newer* than it — which the `SOURCE_CLOCK_SKEW_MINUTES = 2` guard read as the page's clock running ahead and threw the rescue away. Widening the window without this change would have created a new failure mode rather than fixing the old one. `test_a_rescue_is_not_rejected_as_a_timestamp_from_the_future` guards it.
- **`fetched_at` stays pinned to the slot** so the 5-minute grid stays aligned; the moment the reading actually describes is already recorded in `source_updated_at`.

`describe_empty_page()` is why a future failure will be diagnosable: it logs the byte count, the `CMCItem` / `IT` / `ICI` counts taken with parse_counts()'s own patterns, the page's own timestamp, its `<title>` and 200 characters of visible text. That separates an outage at the source from a markup change that broke the parser — a distinction the old bare `no occupancy rows found` could not make. A recovered run additionally prints `recovered on attempt N after Xs`, which is the only measurement of outage length anyone gets.

One of the five failures was unrelated: `actions/checkout` failing TLS verification against github.com, GitHub-side infrastructure, once in 400 runs. Not worth a job-level retry; note it if it recurs.

**The schema lives in two places and must stay in sync.** `fetch_counts.py:connect()` defines the SQLite `occupancy` table; `sql/migrations/001_occupancy.sql` defines the Postgres equivalent plus the `(fetched_at, venue)` unique index that drives `ON CONFLICT`. Adding a column means editing both, plus the `OCCUPANCY_COLUMNS` tuple in `sync_to_postgres.py` — that one tuple now generates the `COPY` list, the `INSERT` list and the `read_sqlite_rows` SELECT, so there is nothing else to update.

**Postgres state is version-controlled, not clicked into a SQL editor.** `sql/migrations/*.sql` is applied in filename order by `tools/apply_migrations.sh`: `001` the table, `002` the views (a symlink to `sql/grafana_weekly_views.sql`, which keeps its own path because tests, docs and `docker-compose.yml` all reference it), `003` the `gym_writer`/`gym_reader` roles, `004` RLS and policies, `005` the read-only panel API in `api_private` plus the two `public` wrappers the browser calls, plus the cost control that guards them (rate-limit policy and counters, and the heatmap cache). Every file is idempotent — and 005 specifically must stay idempotent in a way the others need not be, because it seeds *tunable* rows: its `rate_limit_policy` insert is `ON CONFLICT DO NOTHING` so re-running never overwrites a hand-tuned limit. `docker-compose.yml` mounts `001`, `002` and `005` into `docker-entrypoint-initdb.d` — roles and RLS are skipped there because the initdb hook cannot supply the password psql variables, while `005` is required for the dashboard to render at all. That hook only fires on an empty data directory, so an existing volume needs `tools/apply_migrations.sh` run against the container instead.

**Sync is one psql session, one transaction, and verifies itself.** `sync_to_postgres.py` builds a single script — `BEGIN`, `CREATE TEMP TABLE occupancy_import ... ON COMMIT DROP`, an inline `COPY ... FROM STDIN` payload terminated by `\.`, `INSERT ... ON CONFLICT`, a `DO $$` block that fails the transaction unless every staged row is present with matching values, `COMMIT` — and feeds it to `psql -f -`. Four consequences worth knowing:

- Staging is session-local, so concurrent runs cannot truncate each other's rows. The previous design used a shared `public.occupancy_import` across three connections; interleaved runs silently dropped a reading and still printed success.
- Retries are safe. A commit whose response was lost re-runs, hits the conflict clause, writes nothing, and still passes verification.
- No DDL is needed on `public`, which is what lets the collector run as a SELECT+INSERT-only role.
- `rows_to_csv()` must emit `\n`, not the csv module's default `\r\n`: psql splits the embedded payload into lines itself, and a surviving CR corrupts the last column.

Default conflict strategy is `DO NOTHING` (collection only appends). `--on-conflict update` switches to `DO UPDATE` for backfills and repairs, and needs an owner credential because `gym_writer` has no UPDATE privilege and RLS confines its inserts to roughly the last hour.

**Opening-hours logic is duplicated in five places.** Python-side in `fetch_counts.py:is_open_time()` (Mon-Fri 06-22, Sat 09-22, Sun 09-18); SQL-side in `sql/grafana_weekly_views.sql` (the `open_slots` CTE, using ISODOW 1-7); JS-side in the heatmap panel of `grafana/weekly-dashboard.json` (panel id 2's `OPEN_HOURS_MIN` table, used to label cells outside hours as "closed"); and again in SQL in `api_private.gym_is_open()` (`sql/migrations/005_public_api.sql`), which both the panel-3 filter and the page's staleness signal share. Fifth, in UTC rather than Taipei, in the `pg_cron` schedules of `sql/supabase/001_dispatch_cron.sql`. All five must stay in sync. The SQL sides localize `fetched_at` via `AT TIME ZONE 'Asia/Taipei'` before bucketing.

**休館與開館是兩件事。** 開館時間是每週固定的時段；休館是「這段期間這個場館關了」。後者住在 `closures` 表加兩個 view，定義在 `sql/grafana_weekly_views.sql`（= `002_views.sql`）的最前面。`closure_periods` 是唯一定義——表裡的一次性區間，聯集 `generate_series` 為**兩個具名場館**生成的每月第四個星期一 00:00–17:00。`occupancy_excluding_closures` 是歷史統計該讀的東西。種子有兩筆：健身中心 2026-08-17～09-20 的整修停開（公告 `rent.pe.ntu.edu.tw/news/?K=157`），以及兩館 2026-06-19 端午節整天休館。

- **規則明列場館，不用「NULL = 全館」。** 公告寫的是「健身中心及溫水泳池」，資料也證實兩館皆適用（2026-05-25 兩館都在 17:00 才跳起來：健身中心 0→121、室內游泳池 4→29，對照正常週一同時段 76 / 20）。萬用字元會讓未來新增的場館自動繼承一條從沒對它驗證過的規則。
- **表為什麼在「views」檔案裡。** `weekly_occupancy_slots` 依賴它、而那個 view 是 002；獨立成 `001b_closures.sql` 會讓正確性依賴 shell glob 的 locale collation，而還原演練跑在 ubuntu/glibc、開發在 macOS。
- **為什麼是 view 不是 `is_closed()` 函式。** SQL 函式 body 的未限定名稱是執行時依 caller 的 `search_path` 解析，而 `gym_heatmap()` 是 `SET search_path = ''` 的 SECURITY DEFINER——函式在那裡會找不到 `closures`。View 在建立時就把依賴釘成 OID。同理，這個檔案裡的所有名稱都不加 schema 限定，因為 `test_grafana_weekly_views.py` 會把整份檔案套進臨時 schema。
- **seed 用 `ON CONFLICT DO UPDATE`**，和 005 的 `rate_limit_policy` 相反：限流值是可調參數，closure 是事實，git 該是唯一真相。代價是手動 `UPDATE` 的日期不進 git、不進備份、下次套 migration 會被蓋掉。**改 `ends_at` 或 `reason` 安全；改 `venue` 或 `starts_at` 會插入第二列並留下孤兒**，因為那兩欄就是 conflict key——seed 的註解裡寫了必要的 `DELETE`。`backup.yml` 只 dump `public.occupancy`，closures 靠版本控制還原。
- **歷史統計有三處**，三處都要跟上：`weekly_occupancy_slots`、`current_vs_history` 的 join 側（`latest` 側刻意不濾，那是即時值，休館期間感測器讀到什麼就顯示什麼）、`api_private.gym_panel1_meta_rows`。即時的三個（`gym_panel3_rows`、`gym_panel1_now_rows`、`gym_panel4_rows`）一律照實。
- **`gym_live` 回傳 `activeClosures`**，經由 `api_private.active_closure_rows(p_at)`。那個函式帶時刻參數不是為了通用，是為了可驗證：否則驗第四個星期一只能寫一個平行查詢，而那在 `gym_live` 查錯物件時照樣會過。它在 `api_private`，PostgREST 看不見，所以不需要 RLS 或 anon policy；EXECUTE 仍照 005 的慣例列進 `internal_fns` 一併 revoke（`gym_reader` 也不給——Grafana 不呼叫它）。closure 不走 `gym_heatmap`——那邊快取一小時加瀏覽器六小時，開始與結束會慢到沒有意義。
- **06-19 那筆的 `source` 是 NULL，因為沒有公告可引用**（場館公告頁最舊只到 2025-01-07）。它靠讀數認定：兩館 192 筆開館時段讀數全為 0，而 `source_updated_at` 有 192 個相異值一路爬到 21:55、comfortable/capacity 照常有值——來源頁是活的，在描述一棟空建築。**找它的方法比它本身重要**：用「該場館該星期幾該小時的期望值」對每個場館日評分，並拿已知整修期當正對照，證明偵測器對讀 0（08-19）、讀小非零（08-18 佔期望 12.6%）和半天（05-25 佔 56%）三種都會亮。只找「整天 0」會漏掉 08-18 那種。下一個要補的是中秋節 2026-09-25（週五）。
- **`gym_live` 另外回傳 `windowClosures`**，經由 `api_private.window_closure_rows(p_from, p_to)`，給 panel 3 的折線圖畫休館底色用。**不能用 `activeClosures` 代替**：那個問的是「此刻」，視窗要的是「與這段期間重疊」，而且零寬視窗化約不過去——重疊會排除 closure 開始的那一瞬間，`starts_at <= p_at` 不會。所以是第二個函式而不是加參數。回傳**不裁切**到視窗邊界，因為畫圖時要夾到真實讀數的索引，不是夾到視窗。
- **折線圖照實畫讀數，只是把休館段打上底色。** panel 3 的 x 軸是 ordinal category、一格一筆讀數，所以濾掉休館讀數會連格子一起消失——35 天的整修會被壓成相鄰兩格，圖上看不出中間發生過事。那和「用歷史估計填補缺口」是同一種不誠實的鏡像。`closureBands()` 夾在 `closure-bands:begin/end` 之間讓 `test_public_page_js.py` 用 node 測。
- **它是逐讀數判定再把連續的合併，不是一個 closure 一條 band。** 因為 **closure 真的會重疊**：2026-08-24 既在整修期內又是第四個星期一，健身中心會拿到兩列蓋住同一個早上。一個 closure 畫一條的話那段會被上兩層色（ECharts 會疊加，看得出來變深）、兩個標籤還會疊在同一個位置。逐讀數分組順帶解決了「這個 closure 配不到任何讀數」——它自然什麼都不畫，而不是留下一個 ECharts 會畫在原點的零寬色塊。標籤取涵蓋該段第一筆讀數的那個 closure，而列是按 `starts_at` 排序來的，所以外層的贏過巢狀在裡面的。
- **live card 的休館狀態住在 `grafana/weekly-dashboard.json` 的 panel 4 JS**，因為公開頁面的面板程式碼就是 `extract_panel_js()` 從那裡抽出來的。它讀 `context.closures`——那是 `tools/public_template.html` 的 `makeContext()` 才有的欄位，Grafana 讀到 `undefined` 就走原路徑。**檔案會變，Grafana 畫面不會**（已實測）。
- **日期字串由 `closureSpan()` 算一次**，包在 `closure-span:begin/end` 標記之間讓 `test_public_page_js.py` 抽出來用 node 測（沒有 node 就 skip）。兩個坑都踩過：整日區間與當日區間共用公式會把每月休館渲染成「24 Aug – 23 Aug」；用 `Intl` 的 `month: "short"` 則會在現行 ICU 上輸出 `Sept`、舊版輸出 `Sep`，**同一個頁面在不同瀏覽器顯示不同文字**。所以月份用固定陣列、時間用 `hourCycle: "h23"`。
- **頁面會拿 `Date.now()` 重濾一次 payload**，因為 `applyLive()` 也會被 localStorage 的舊快取呼叫；並且對 `ends_at` 排精確的消失計時器（鏈式，因為 `setTimeout` 超過約 24.8 天會飽和）。反向做不到——API 只回傳當下生效的區間，所以 closure **開始**最多慢一個輪詢週期。

**Weekday opening really is 06:00, not the 08:00 the website advertises.** `rent.pe.ntu.edu.tw` lists 08:00-22:00 for the 綜合體育館 *building*, but 健身中心 is populated from 06:00 on every weekday in the collected history (06:00 typically 6-17 people, 07:00 typically 19-34). Commit `28a4ae6` corrected this on purpose; "fixing" it back to 08:00 discards two hours of real data every weekday.

**Timezone is pinned, never inherited.** `fetch_counts.py` uses `now_taipei()` (`zoneinfo.ZoneInfo("Asia/Taipei")`), not `datetime.now().astimezone()`. `is_open_time()` compares bare hour numbers, so on a UTC host — every GitHub Actions runner — inheriting the host clock would make `--open-hours-only` skip nearly every scheduled run without erroring. Stored rows carry an explicit `+08:00` offset and the Postgres column is `TIMESTAMPTZ`, so this affects decisions, not stored history.

**Grafana dashboard is wired to a stable datasource UID `gym-postgres`.** `grafana/weekly-dashboard.json` references this UID and `grafana/provisioning/datasources/datasources.yml` creates it at Grafana startup. The dashboard JSON is the **bare dashboard model** (no `{"dashboard": {...}, "overwrite": true}` wrapper) because file provisioning expects that shape; do not re-wrap it. The dashboard requires the `volkovlabs-echarts-panel` plugin (auto-installed via `GF_INSTALL_PLUGINS` in `docker-compose.yml`) and exposes two extra template variables, `${granularity}` (`30min`/`60min`, default `60min`) and `${palette}` (`terracotta`/`sage`/`slate`/`rose`/`teal`/`lavender`, default `slate`), that are read inside the ECharts panel JS to switch SQL bucketing and color stops at render time.

**Grafana picks up dashboard edits from disk.** `grafana/provisioning/dashboards/providers.yml` watches `/var/lib/grafana/dashboards` (bind-mounted from `grafana/weekly-dashboard.json`) every 10 s. Save the JSON, wait ten seconds, reload the browser — no curl POST. `allowUiUpdates: false` keeps the UI editor read-only; flip to `true` and `docker compose restart grafana` if you want the UI to be editable again.

**The dashboard has four panels stacked top-to-bottom:** (1) `Popular Times` bar chart driven by `weekly_occupancy_slots` filtered to today's `ISODOW`, with the current hour highlighted via `palette[5]`; (4) `Live Cards` two-up with sparkline of today's `occupancy` readings and a delta-vs-historical-median row driven by `current_vs_history`; (2) `Weekly 30-Minute Occupancy Heatmap`; (3) `Selected Venue Exact Counts` line chart. Panel 1 and 4 both read `${venue}` and `${palette}` only — no new template variables. Panel titles and all in-panel labels are English; venue names stay Chinese (source data). The public page (`tools/render_public_html.py` builds the shell) maps venue names to English display names in the browser; Chinese names stay the keys everywhere, including the `<option value>` of the venue selector.

**SSL workaround is intentional, not a bug.** `fetch_counts.py:make_ssl_context()` clears `VERIFY_X509_STRICT` because the NTU institutional cert chain omits Subject Key Identifier extensions that OpenSSL 3.x strict mode rejects. Don't replace it with an unverified context — full chain + hostname verification is still on.

**`fetch_counts.py` redirects `sys.stdout`/`sys.stderr` to a `DailyLogStream`** unless `--no-log-file` is set. This writes to `logs/<YYYY-MM-DD>.log` in addition to the terminal, so anything you print from inside long-running code shows up there. `logs/` is git-ignored.

## Conventions worth knowing

- Timestamps stored as ISO strings with timezone (`datetime.now().astimezone().replace(second=0, microsecond=0).isoformat()`). Rows are aligned to the minute, not the second.
- SQLite `.bak-*` files in the repo root are produced by `rsync.sh` — they're snapshots of the previous local DB before an rsync overwrote it. Safe to delete; git-ignored.
- Don't commit `*.sqlite3`, `*.csv`, or anything under `logs/` — all ignored.
- Cron jobs run through `tools/cronlog.sh`, which writes per-day logs to `logs/<YYYY-MM>/<YYYY-MM-DD>-<name>.log` (`<name>` = `cron`/`sync`/`render`). The cron fetch passes `--no-log-file` so `fetch_counts.py`'s own `DailyLogStream` (`logs/<YYYY-MM-DD>.log`) is left for interactive runs only.
- Plans/specs from the `superpowers` workflow live under `docs/superpowers/` and the brainstorming scratch under `.superpowers/brainstorm/` (ignored).
- Deferred features and undecided design calls are tracked in `docs/TODO.md` (the feature backlog) — consult it before starting feature work, and move items out when they ship.
