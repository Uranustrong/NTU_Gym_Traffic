# Gym Fetch

Fetch current NTU sports center occupancy from <https://rent.pe.ntu.edu.tw/> and store it locally for later history analysis.

## Environment

This project only uses Python standard-library modules. A virtual environment is optional, not required.

```bash
cd "/Users/songhejun/Downloads/My_Project/Fetch_Gym"
python3 --version
chmod +x fetch_counts.py
```

The local project path is `/Users/songhejun/Downloads/My_Project/Fetch_Gym`. The examples keep it quoted so they stay safe if the project is moved to a path with spaces later.

`requirements.txt` is kept so the project still has a normal Python shape, but there is nothing to install right now. If you prefer using a virtual environment anyway:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

If Python fails with `CERTIFICATE_VERIFY_FAILED` and mentions `Missing Subject Key Identifier`, the script already works around that by disabling OpenSSL strict certificate-chain checks while keeping normal certificate and hostname verification enabled.

## Try One Fetch

```bash
python3 fetch_counts.py
```

It creates `gym_counts.sqlite3` and appends one row per venue.

Inspect saved records:

```bash
sqlite3 gym_counts.sqlite3 \
  'SELECT fetched_at, source_updated_at, venue, current_count FROM occupancy ORDER BY fetched_at DESC LIMIT 10;'
```

Export CSV:

```bash
python3 fetch_counts.py --csv occupancy.csv
```

## Run Every 5 Minutes With cron

The sports center opening hours are:

- Monday-Friday: 08:00-22:00
- Saturday: 09:00-22:00
- Sunday: 09:00-18:00

Use `crontab -e` and add these lines. They run on exact 5-minute boundaries during opening hours.

```cron
*/5 8-21 * * 1-5 cd "/Users/songhejun/Downloads/My_Project/Fetch_Gym" && /usr/bin/python3 fetch_counts.py --open-hours-only >/dev/null 2>&1
*/5 9-21 * * 6 cd "/Users/songhejun/Downloads/My_Project/Fetch_Gym" && /usr/bin/python3 fetch_counts.py --open-hours-only >/dev/null 2>&1
*/5 9-17 * * 0 cd "/Users/songhejun/Downloads/My_Project/Fetch_Gym" && /usr/bin/python3 fetch_counts.py --open-hours-only >/dev/null 2>&1
```

This records 08:00 through 21:55 on weekdays, 09:00 through 21:55 on Saturday, and 09:00 through 17:55 on Sunday. It does not fetch at the closing minute itself.

Fetch output is written to daily files in `logs/`, such as `logs/2026-05-14.log`.

Check whether cron is writing data:

```bash
tail -f "logs/$(date +%F).log"
sqlite3 gym_counts.sqlite3 'SELECT COUNT(*) FROM occupancy;'
```

## Run With tmux

`tmux` is useful when you want to start a long-running fetch loop manually, keep it alive after disconnecting SSH, and come back later to inspect the output.

Start a named tmux session:

```bash
tmux new -s gym-fetch
cd "/Users/songhejun/Downloads/My_Project/Fetch_Gym"
```

Inside tmux, run the built-in loop. It waits until the next exact 5-minute boundary before fetching. During closed hours, it waits until the next opening time instead of waking up every 5 minutes.

```bash
python3 fetch_counts.py --loop --open-hours-only
```

If the site returns a temporary page without occupancy data, the script retries twice with a 10-second delay before skipping that timestamp.

Detach from tmux without stopping the loop:

```text
Ctrl-b d
```

Come back to the running session:

```bash
tmux attach -t gym-fetch
```

Stop the loop from inside tmux:

```text
Ctrl-c
```

Or stop the whole tmux session from outside:

```bash
tmux kill-session -t gym-fetch
```

Check whether data is still being saved:

```bash
tail -f "logs/$(date +%F).log"
sqlite3 gym_counts.sqlite3 'SELECT fetched_at, venue, current_count FROM occupancy ORDER BY id DESC LIMIT 10;'
```

For long-term unattended collection, `cron` is usually more reliable because it starts a fresh job every 5 minutes and can survive reboots if cron is enabled. `tmux` is better for interactive testing and short-running experiments.

## Visualize With Grafana and PostgreSQL

The stack runs as a `docker compose` project: Grafana + Postgres in one command, all configuration tracked in git. Grafana provisions its datasource and dashboard from files in this repo, so a fresh checkout becomes a working dashboard with no manual UI clicks.

- Grafana runs at <http://localhost:3000>
- PostgreSQL is exposed to the host at `127.0.0.1:5433`
- Inside the compose network Grafana reaches Postgres as `postgres:5432`
- The dashboard JSON is bind-mounted from `grafana/weekly-dashboard.json` and auto-reloaded every 10 s
- Plugin `volkovlabs-echarts-panel` 7.2.4 is auto-installed at container start

### Prereqs

- Docker engine (Docker Desktop, OrbStack, colima, or native Linux Docker). Compose v2 ships with all of them.
- `psql` is **not** required on the host — the sync script can shell into the container.

### Bring the stack up

1. Copy `.env.example` to `.env` and edit the passwords (`.env` is git-ignored):

   ```bash
   cp .env.example .env
   # edit GRAFANA_ADMIN_PASSWORD, POSTGRES_PASSWORD
   ```

2. (Migrating from the old manual setup) Remove any pre-existing standalone containers so they don't clash on ports 3000 / 5433:

   ```bash
   docker rm -f gym-grafana gym-postgres 2>/dev/null || true
   ```

   Old volumes (`gym-grafana-data`, `gym-postgres-data`) are left alone as a manual backup; compose creates project-prefixed volumes (`fetch_gym_gym-grafana-data`, `fetch_gym_gym-postgres-data`).

3. Bring up the stack. First run is slower because postgres seeds its schema and Grafana downloads the plugin.

   ```bash
   docker compose up -d
   ```

   On first start Postgres runs `sql/init/01_schema.sql` (creates the `occupancy` table) and `sql/grafana_weekly_views.sql` (creates the views) from the `/docker-entrypoint-initdb.d/` hook. These run **only when the data volume is empty** — later restarts skip them.

4. Populate the database from the remote collector:

   ```bash
   ./rsync.sh pull-data
   ```

   `rsync.sh` auto-sources `.env`, so `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` come from there; the URL `postgresql://<user>:<pass>@127.0.0.1:5433/<db>` is constructed on the fly.

5. Open <http://localhost:3000/d/gym-weekly-display/gym-weekly-occupancy>. Log in with the credentials from `.env`.

### Updating the dashboard

Edit `grafana/weekly-dashboard.json`, save, and Grafana picks up the new version within ten seconds — no curl POST, no UI re-import. Use `tools/verify_dashboard.py` to confirm the dashboard still renders without errors:

```bash
python3 tools/verify_dashboard.py
```

The script reads `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` from `.env`.

The dashboard JSON in this repo is the **bare dashboard model** (no `{"dashboard": {...}, "overwrite": true}` wrapper), because that's what file provisioning expects. The old curl-import API workflow no longer works against this file as-is.

The provisioning provider sits in `grafana/provisioning/dashboards/providers.yml`. To allow UI edits to persist back to the running container (separate from the file on disk), flip `allowUiUpdates` to `true` there and `docker compose restart grafana`.

### Inspect what got imported

```bash
docker exec gym-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT fetched_at, venue, current_count FROM occupancy ORDER BY fetched_at DESC LIMIT 10;"'
```

The sync script (`sync_to_postgres.py`) uses Python standard library plus the host `psql` if present; otherwise pass `--psql tools/psql_gym_postgres.sh` to wrap through the docker container.

Rows are upserted on `(fetched_at, venue)` so re-running the sync is idempotent.

### Create Grafana Panels

After the PostgreSQL datasource is saved, create a dashboard:

1. Open <http://localhost:3000>.
2. Go to `Dashboards`.
3. Click `New` -> `New dashboard`.
4. Click `Add visualization`.
5. Select the PostgreSQL datasource you just created.

Grafana may open the PostgreSQL query editor in visual builder mode. If you do not see a place to paste SQL, look near the query editor toolbar for `Code`, `Edit SQL`, `Raw SQL`, or a pencil/code icon. Switch to code mode, then paste the SQL query.

Suggested dashboard variables:

- `venue`: query variable, `SELECT DISTINCT venue FROM occupancy ORDER BY venue;`, use single-select so the weekly table can keep venue out of the grid.
- `metric`: custom variable, values `avg_count,crowding_ratio`, default `avg_count`.

Weekly 30-minute heatmap table:

```sql
WITH metric_slots AS (
  SELECT
    weekday_number,
    slot_start_time,
    slot_label,
    CASE
      WHEN '${metric}' = 'crowding_ratio' THEN avg_crowding_ratio
      ELSE avg_current_count
    END AS value
  FROM weekly_occupancy_slots
  WHERE venue = ${venue:sqlstring}
)
SELECT
  slot_label AS "Time",
  MAX(value) FILTER (WHERE weekday_number = 1) AS "Mon",
  MAX(value) FILTER (WHERE weekday_number = 2) AS "Tue",
  MAX(value) FILTER (WHERE weekday_number = 3) AS "Wed",
  MAX(value) FILTER (WHERE weekday_number = 4) AS "Thu",
  MAX(value) FILTER (WHERE weekday_number = 5) AS "Fri",
  MAX(value) FILTER (WHERE weekday_number = 6) AS "Sat",
  MAX(value) FILTER (WHERE weekday_number = 7) AS "Sun"
FROM metric_slots
GROUP BY slot_start_time, slot_label
ORDER BY slot_start_time;
```

For this panel, use a `Table` visualization and enable colored cell backgrounds on the weekday columns. Keep `venue` as a single-select dashboard variable so the table can use weekdays as columns and time slots as rows.

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
  AND venue = ${venue:sqlstring}
ORDER BY fetched_at;
```

For line charts, set the panel visualization type to `Time series`.

Current occupancy by venue:

```sql
SELECT
  fetched_at AS "time",
  venue AS metric,
  current_count AS value
FROM occupancy
WHERE $__timeFilter(fetched_at)
ORDER BY fetched_at;
```

This creates one line per venue. The X-axis is `fetched_at`, and the Y-axis is `current_count`.

Average occupancy by hour:

```sql
SELECT
  date_trunc('hour', fetched_at) AS "time",
  venue AS metric,
  ROUND(AVG(current_count), 1) AS value
FROM occupancy
WHERE $__timeFilter(fetched_at)
GROUP BY 1, venue
ORDER BY 1, venue;
```

This smooths the chart into hourly buckets. It is useful once you have collected data for multiple days.

Capacity usage percentage:

```sql
SELECT
  fetched_at AS "time",
  venue AS metric,
  ROUND(current_count * 100.0 / NULLIF(capacity_count, 0), 1) AS value
FROM occupancy
WHERE $__timeFilter(fetched_at)
ORDER BY fetched_at;
```

For this panel, set the unit to `Percent (0-100)` in the panel options.

Recent records table:

```sql
SELECT
  fetched_at,
  venue,
  current_count,
  comfortable_count,
  capacity_count
FROM occupancy
ORDER BY fetched_at DESC, venue
LIMIT 50;
```

For this panel, set the visualization type to `Table`.

Useful panel settings:

- Time range: start with `Last 6 hours` or `Last 24 hours`.
- Legend: show `metric` or `venue` so each line is labeled.
- Tooltip: use `All` mode to compare venues at the same timestamp.
- Refresh: set `5m` or `10m` if you keep syncing PostgreSQL regularly.

### Keep PostgreSQL Updated

After new rows are fetched into `gym_counts.sqlite3`, run the sync command again:

```bash
./rsync.sh pull-data
```

For periodic local syncing, add a cron entry such as:

```cron
*/10 * * * * cd "/Users/songhejun/Downloads/My_Project/Fetch_Gym" && ./rsync.sh pull-data >> postgres-sync.log 2>&1
```

### Docker Cleanup

Stop the stack without deleting data:

```bash
docker compose stop
```

Start it again later:

```bash
docker compose start
```

Tear down containers but keep volumes (data survives):

```bash
docker compose down
```

Delete saved data too (irreversible):

```bash
docker compose down -v
```

Force a re-run of the `/docker-entrypoint-initdb.d/` scripts (e.g. after editing `sql/init/01_schema.sql` or `sql/grafana_weekly_views.sql`) — only the postgres data volume needs wiping:

```bash
docker compose down
docker volume rm fetch_gym_gym-postgres-data
docker compose up -d
./rsync.sh pull-data   # repopulate
```

### Clean Up Homebrew PostgreSQL

If you previously started PostgreSQL with Homebrew, stop it so it does not compete for port `5432`:

```bash
brew services stop postgresql@18
```

Check its status:

```bash
brew services list
```

If you created a Homebrew-backed `gym_fetch` database and want to delete it before stopping the server, run:

```bash
dropdb gym_fetch
brew services stop postgresql@18
```

## Useful Queries

Average occupancy by hour:

```bash
sqlite3 gym_counts.sqlite3 '
SELECT venue, strftime("%H", fetched_at) AS hour, ROUND(AVG(current_count), 1) AS avg_people
FROM occupancy
GROUP BY venue, hour
ORDER BY venue, hour;
'
```

Recent records:

```bash
sqlite3 gym_counts.sqlite3 '
SELECT fetched_at, venue, current_count
FROM occupancy
ORDER BY fetched_at DESC
LIMIT 20;
'
```

## SSL Strict Mode Note

The site can work with `curl` but fail in Python with an error like:

```text
CERTIFICATE_VERIFY_FAILED: Missing Subject Key Identifier
```

This happens because recent Python/OpenSSL combinations may enable stricter X.509 certificate-chain checks. One of those strict checks expects certificate-chain extensions such as Subject Key Identifier to be present. Some institutional websites still serve certificate chains that browsers and `curl` accept, but OpenSSL strict mode rejects.

The script handles this in `make_ssl_context()`:

```python
context = ssl.create_default_context()
context.verify_flags &= ~ssl.VERIFY_X509_STRICT
```

This does not disable HTTPS verification. Python still verifies the certificate chain against trusted CAs and still checks that the hostname matches `rent.pe.ntu.edu.tw`. It only disables OpenSSL's extra strict extension checks, which is narrower and safer than using an unverified SSL context.
