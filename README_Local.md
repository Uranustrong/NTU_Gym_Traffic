# Gym Fetch

Fetch current NTU sports center occupancy from <https://rent.pe.ntu.edu.tw/> and store it locally for later history analysis.

## Environment

This project only uses Python standard-library modules. A virtual environment is optional, not required.

```bash
cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Code/Fetch_Gym"
python3 --version
chmod +x fetch_counts.py
```

The local project path contains a space (`Mobile Documents`), so keep the path quoted in shell commands and cron entries.

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
*/5 8-21 * * 1-5 cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Code/Fetch_Gym" && /usr/bin/python3 fetch_counts.py --open-hours-only >/dev/null 2>&1
*/5 9-21 * * 6 cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Code/Fetch_Gym" && /usr/bin/python3 fetch_counts.py --open-hours-only >/dev/null 2>&1
*/5 9-17 * * 0 cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Code/Fetch_Gym" && /usr/bin/python3 fetch_counts.py --open-hours-only >/dev/null 2>&1
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
cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Code/Fetch_Gym"
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

PostgreSQL is the recommended Grafana datasource for this project. Grafana includes PostgreSQL support by default, and the data stays queryable with normal SQL.

The simplest local setup is to run both Grafana and PostgreSQL in Docker:

- Grafana runs at <http://localhost:3000>
- PostgreSQL is exposed to macOS at `127.0.0.1:5433`
- Grafana connects to PostgreSQL through the Docker network as `gym-postgres:5432`

Create a Docker network:

```bash
docker network create gym-net
```

If Docker says `network with name gym-net already exists`, keep going.

Start PostgreSQL:

```bash
docker run -d \
  --name gym-postgres \
  --network gym-net \
  -p 5433:5432 \
  -e POSTGRES_USER=songhejun \
  -e POSTGRES_PASSWORD=gym_fetch_dev \
  -e POSTGRES_DB=gym_fetch \
  -v gym-postgres-data:/var/lib/postgresql \
  postgres:18
```

Start Grafana:

```bash
docker run -d \
  --name gym-grafana \
  --network gym-net \
  -p 3000:3000 \
  -v gym-grafana-data:/var/lib/grafana \
  grafana/grafana
```

Open Grafana at <http://localhost:3000>. The default login is:

```text
Username: admin
Password: admin
```

Sync the SQLite history into Docker PostgreSQL:

```bash
python3 sync_to_postgres.py \
  --sqlite-db gym_counts.sqlite3 \
  --postgres-url postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch
```

Apply the weekly dashboard views:

```bash
psql postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch \
  -v ON_ERROR_STOP=1 \
  -f sql/grafana_weekly_views.sql
```

The sync script uses Python standard-library modules plus the `psql` command-line client. No Python PostgreSQL package is required. It creates these PostgreSQL tables if needed:

- `occupancy`: the Grafana-facing table
- `occupancy_import`: a staging table used during sync

Rows are upserted by `(fetched_at, venue)`, so running the sync multiple times updates existing rows and inserts new ones.

Inspect imported rows:

```bash
psql postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch \
  -c 'SELECT fetched_at, venue, current_count FROM occupancy ORDER BY fetched_at DESC LIMIT 10;'
```

In Grafana, add a PostgreSQL datasource with:

```text
Host URL: gym-postgres:5432
Database name: gym_fetch
Username: songhejun
Password: gym_fetch_dev
TLS/SSL mode: disable
```

### Create Grafana Panels

After the PostgreSQL datasource is saved, create a dashboard:

1. Open <http://localhost:3000>.
2. Go to `Dashboards`.
3. Click `New` -> `New dashboard`.
4. Click `Add visualization`.
5. Select the PostgreSQL datasource you just created.

Grafana may open the PostgreSQL query editor in visual builder mode. If you do not see a place to paste SQL, look near the query editor toolbar for `Code`, `Edit SQL`, `Raw SQL`, or a pencil/code icon. Switch to code mode, then paste the SQL query.

Suggested dashboard variables:

- `venue`: query variable, `SELECT DISTINCT venue FROM occupancy ORDER BY venue;`, enable multi-value.
- `metric`: custom variable, values `avg_count,crowding_ratio`, default `avg_count`.

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

For this panel, use a `Table` visualization and enable colored cell backgrounds on `value`. This gives a weekday-by-time-slot heatmap while keeping sample counts visible.

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
rsync -avhz b12902066@ws7.csie.ntu.edu.tw:/tmp2/b12902066/Gym_Fetch/ ./
python3 sync_to_postgres.py \
  --sqlite-db gym_counts.sqlite3 \
  --postgres-url postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch
```

For periodic local syncing, add a cron entry such as:

```cron
*/10 * * * * cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Code/Fetch_Gym" && /usr/bin/python3 sync_to_postgres.py --sqlite-db gym_counts.sqlite3 --postgres-url postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch >> postgres-sync.log 2>&1
```

### Docker Cleanup

Stop the dashboard stack without deleting data:

```bash
docker stop gym-grafana gym-postgres
```

Start it again later:

```bash
docker start gym-postgres gym-grafana
```

Remove the containers but keep their named volumes:

```bash
docker rm gym-grafana gym-postgres
```

Delete the saved PostgreSQL and Grafana data only when you are sure you no longer need it:

```bash
docker volume rm gym-postgres-data gym-grafana-data
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
