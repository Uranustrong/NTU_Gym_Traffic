# Server Setup on a CSIE Workstation (ws7)

Working document for running the entire stack — fetch collector, Postgres, Grafana, and a public-facing read-only dashboard — on a CSIE NASA-Lab workstation. The workstation policy requires **rootless Docker**, and Grafana cannot be exposed directly to the internet, so the public endpoint is a self-contained interactive HTML page (regenerated every 5 minutes by cron) served by the workstation's nginx `UserDir`.

## What you'll end up with

```
┌── ws7.csie.ntu.edu.tw ───────────────────────────────────────────┐
│                                                                  │
│  cron ─▶ fetch_counts.py ─▶ gym_counts.sqlite3                   │
│                                       │                          │
│  cron ─▶ tools/sync_local.sh ─────────┘                          │
│                                       │                          │
│                                       ▼                          │
│              Rootless Docker (data-root = /tmp2/b12902066/docker)     │
│   ┌────────────────────────┐   ┌────────────────────────────┐    │
│   │ gym-postgres :5433     │◀─▶│ gym-grafana :3000          │    │
│   │ (127.0.0.1 only)       │   │ (127.0.0.1 only — your own │    │
│   └───────────┬────────────┘   │  SSH-tunnel exploration)   │    │
│               │                └────────────────────────────┘    │
│               │ queries                                          │
│  cron ─▶ tools/render_public_html.py                             │
│               │  (pure stdlib + psql; bakes data + the same      │
│               │   ECharts panel code into one static HTML)       │
│               ▼                                                  │
│                       ~/public_html/gym/                         │
│                       ├── index.html    (interactive, dark)      │
│                       └── echarts.min.js (bundled, no CDN)       │
└──────────────────────────────────────┬───────────────────────────┘
                                       │ nginx UserDir
                                       ▼
              https://www.csie.ntu.edu.tw/~b12902066/gym/
              (public, interactive, regenerated every 5 min)
```

The public page is **not** a screenshot — it is a real interactive page. It
re-uses the exact ECharts panel code from `grafana/weekly-dashboard.json`, so
visitors get palette switching, venue switching, and hover tooltips. What it
does not have (versus live Grafana) is arbitrary time-range scrubbing — the
data window is fixed at render time. Grafana itself stays internal: you reach
it over an SSH tunnel for your own deeper exploration.

Paths and commands below are written for the account `b12902066`. If you deploy under a different CSIE account, substitute it throughout.

---

## Step 0 · Prerequisites

```bash
ssh b12902066@ws7.csie.ntu.edu.tw
whoami            # confirm you're b12902066
echo "$HOME"      # confirm /home/b12902066
df -h /tmp2       # confirm /tmp2/b12902066 exists with enough free space
```

You should have ~5 GB free under `/tmp2/b12902066` (postgres data, grafana data, docker images, plus a few months of occupancy history live well under 1 GB total — the rest is image cache and headroom).

---

## Step 1 · Install rootless Docker

Follows the NASA-Lab workstation guide: <https://nasalab.csie.ntu.edu.tw/workstation/docker_tutorial.html>

```bash
# 1a. enable systemd linger so the docker user service survives logout
linger-switch enable

# 1b. allocate subuid/subgid ranges for the rootless runtime
subuid-register

# 1c. install rootless docker INTO /tmp2/b12902066/docker/bin (so it lives on
#     fast local storage, not your home quota)
mkdir -p /tmp2/b12902066/docker/bin
curl -fsSL https://get.docker.com/rootless | DOCKER_BIN=/tmp2/b12902066/docker/bin sh
```

Append the following to `~/.bashrc` (or `~/.zshrc` if you use zsh) and `source` it:

```bash
export PATH=/tmp2/b12902066/docker/bin:$PATH
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock
```

Tell docker to keep its data on `/tmp2` instead of `~/.local/share/docker` (which would eat your home quota):

```bash
mkdir -p ~/.config/docker
cat > ~/.config/docker/daemon.json <<'EOF'
{
  "data-root": "/tmp2/b12902066/docker"
}
EOF
systemctl --user daemon-reload
systemctl --user restart docker
```

Verify:

```bash
docker version           # client + server both report a version
docker info | grep -i rootless   # confirms `Cgroup Version: 2` and `rootless: true`
docker compose version   # v2 ships with the rootless install
```

If `systemctl --user start docker` fails with "Failed to connect to bus", check that linger is enabled (`loginctl show-user b12902066 | grep Linger`).

---

## Step 2 · Clone the repo

```bash
cd /tmp2/b12902066
git clone <your-git-remote> Gym_Fetch   # e.g. git@github.com:<user>/Gym_Fetch.git
cd Gym_Fetch
```

If you previously rsynced the code in but never committed it, push it to a private GitHub repo first from your Mac:

```bash
# on the Mac (local)
gh repo create Gym_Fetch --private --source=. --push
```

Then `git clone` on ws7 as above. The CSIE workstation has outbound HTTPS so HTTPS or SSH (with key uploaded) both work.

---

## Step 3 · Configure `.env`

```bash
cp .env.example .env
```

Edit `.env` and set values appropriate for the server:

```ini
POSTGRES_USER=songhejun
POSTGRES_PASSWORD=<a strong random password>
POSTGRES_DB=gym_fetch
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=<a strong random password — only you log in with this>
```

The public page is generated straight from Postgres and never touches
Grafana, so `GRAFANA_AUTH_ANONYMOUS_ENABLED` is **not** required — leave it
off.

`.env` is in `.gitignore`; never commit it. Quoting is not required even if your password contains `(`, `)`, `$`, etc. — the loaders parse line-by-line.

---

## Step 4 · Bring up the containers

The `docker-compose.yml` defines two services with **different roles**:

| Service | Role | Required? |
|---|---|---|
| `gym-postgres` | The database. Both the collector sync and the public page read from it. | **Required** — everything depends on it. |
| `gym-grafana` | Your private interactive dashboard, reached over an SSH tunnel. | **Optional** — the public page never uses it. |

### 4a. Postgres — required

```bash
docker compose up -d postgres
```

First run pulls `postgres:18` and runs `sql/init/01_schema.sql` + `sql/grafana_weekly_views.sql` from the `/docker-entrypoint-initdb.d/` hook to bootstrap the `occupancy` table and the views. Verify:

```bash
docker compose ps        # gym-postgres Up, healthy
docker exec gym-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"   # 2 tables
docker exec gym-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dv"   # 2 views
```

If you only want the public page and never need Grafana on the server, you can stop here and skip 4b — Steps 5–7 do not depend on Grafana.

### 4b. Grafana — optional (your private dashboard)

```bash
docker compose up -d          # no service name = brings up everything, incl. Grafana
```

First run also pulls `grafana/grafana:13.0.1` and installs the `volkovlabs-echarts-panel` 7.2.4 plugin. Verify:

```bash
docker compose ps        # gym-grafana now also Up
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3000/api/health   # 200
```

Grafana binds to `127.0.0.1:3000` only — it is not reachable from outside the workstation. To use it, open an SSH tunnel from your own machine:

```bash
ssh -N -L 3000:127.0.0.1:3000 b12902066@ws7.csie.ntu.edu.tw
# then browse http://localhost:3000 on your laptop
```

To later turn Grafana off without touching Postgres:

```bash
docker compose stop grafana
```

---

## Step 5 · Backfill occupancy data

Two paths depending on whether you have prior history to import:

**5a. Starting from scratch.** Skip to Step 6; cron will start collecting on its own and the public page will populate over the following days.

**5b. Importing existing data.** If you have a populated `gym_counts.sqlite3` (e.g. carried over from a previous workstation deployment), copy it into the repo dir and run the sync wrapper once:

```bash
cp /path/to/existing/gym_counts.sqlite3 /tmp2/b12902066Gym_Fetch/gym_counts.sqlite3
./tools/sync_local.sh
```

You should see `synced N rows from ... to PostgreSQL`.

---

## Step 6 · Set up cron

```bash
# inspect / edit
cd /tmp2/b12902066Gym_Fetch
$EDITOR crontab.example       # replace b12902066 with your account
crontab crontab.example
crontab -l                    # confirm it's installed
```

`crontab.example` installs three jobs:

1. `fetch_counts.py` every 5 min during open hours (Mon-Fri 06-22, Sat 09-22, Sun 09-18)
2. `tools/sync_local.sh` every 10 min (offset 2 min) — pushes new SQLite rows into Postgres
3. `tools/render_public_html.py` every 5 min — regenerates the public page

Tail the logs to confirm jobs run as expected:

```bash
tail -f logs/cron.log logs/sync.log logs/render.log
```

---

## Step 7 · Set up the public page

The public page generator (`tools/render_public_html.py`) is pure Python standard library plus the `psql` CLI — **no Playwright, no headless browser, no extra installs**. It queries Postgres, bakes the data and the ECharts panel code into one `index.html`, and ships a bundled `echarts.min.js` (no external CDN).

### 7a. Create the public directory

```bash
mkdir -p ~/public_html/gym
chmod 711 ~                                 # nginx needs +x on $HOME
chmod 755 ~/public_html ~/public_html/gym
```

### 7b. Trigger one render manually

```bash
cd /tmp2/b12902066Gym_Fetch
python3 tools/render_public_html.py --output-dir ~/public_html/gym --psql tools/psql_gym_postgres.sh
ls -la ~/public_html/gym
# expect: index.html  echarts.min.js
```

`--psql tools/psql_gym_postgres.sh` wraps queries through the postgres
container, so the host does not need a `psql` client installed. (If the host
*does* have `psql`, you can drop the flag.) The wrapper reads `DOCKER_BIN`,
`POSTGRES_USER`, and `POSTGRES_DB` from the environment — `sync_local.sh`
already sources `.env`; for a bare manual run, export `DOCKER_BIN` if `docker`
is not on `PATH`.

### 7c. Verify the public URL

Open in a browser (or `curl -I`):

```
https://www.csie.ntu.edu.tw/~b12902066/gym/
```

Expected: a dark interactive dashboard — venue / palette / granularity / metric dropdowns all work, hover tooltips work, and the page auto-reloads every 5 minutes via the HTML `<meta http-equiv="refresh">` tag.

### 7d. Two-layer refresh — how the page stays current

There are two independent refresh cycles; both must run for new data to reach a visitor:

1. **Data → `index.html`** — `tools/render_public_html.py`, run by cron every 5 minutes (Step 6). This is the step that actually re-queries Postgres. The data in `index.html` is *baked in* at render time; the file does not query the database when a browser loads it.
2. **`index.html` → browser** — the `<meta http-equiv="refresh" content="300">` tag makes the visitor's browser re-fetch the file every 5 minutes.

So a fresh occupancy reading reaches a public visitor within roughly 5–10 minutes: collector → sqlite → (sync cron) → Postgres → (render cron) → `index.html` → (browser refresh) → screen. If you ever edit data and the page seems stale, it just means the render cron has not run yet — trigger `render_public_html.py` by hand to force it.

### 7e. Cron is already wired (Step 6)

The third cron line re-renders the page every 5 minutes. From now on `~/public_html/gym/` stays current as long as docker + cron are running.

---

## Operations

### Restart the stack after a workstation reboot

Because we enabled `linger`, the docker user service starts automatically on boot — and `docker-compose.yml` has `restart: unless-stopped`, so containers come back up too. If they don't:

```bash
systemctl --user start docker
cd /tmp2/b1290206/6Gym_Fetch
docker compose up -d postgres   # required
docker compose up -d            # add this too if you also run Grafana (4b)
```

### Update the dashboard (panel edits, palette changes, etc.)

The repo's `grafana/weekly-dashboard.json` is bind-mounted into the running Grafana. Edit on the Mac, commit, then on ws7:

```bash
cd /tmp2/b12902066Gym_Fetch
git pull
```

File-provisioning picks up the new JSON within 10 seconds — no container restart needed. The public page also re-extracts the panel code from `grafana/weekly-dashboard.json` on its next render (within 5 minutes), so a dashboard edit propagates to both Grafana and the public page automatically.

### Update postgres views

The `sql/init/*` and `sql/grafana_weekly_views.sql` files only run on **first** container start (data volume empty). To force re-apply after editing a view:

```bash
docker exec -i gym-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 < sql/grafana_weekly_views.sql
```

To force re-apply the schema you have to wipe the postgres volume and rebackfill — usually overkill; targeted SQL via `docker exec` is enough.

### Pulling data back to the Mac for local-dev

`rsync.sh pull-data` on the Mac still works; it `sqlite3 .backup`s the remote DB, rsyncs it down, and syncs to the Mac's local Postgres for local Grafana iteration. Nothing about ws7 setup breaks this.

### Stopping cleanly

```bash
# stop containers but keep data:
docker compose stop
# tear down but keep volumes:
docker compose down
# nuke everything including data:
docker compose down -v
```

### Disk usage check

```bash
du -sh /tmp2/b12902066/docker
/docker system df    # per-image / per-volume breakdown
```

If docker bloats over time:

```bash
docker image prune -a
docker volume prune
```

---

## Security notes

- **`127.0.0.1` binding.** Both Postgres (`5433`) and Grafana (`3000`) bind to the loopback interface only. Other students on the same workstation **cannot** connect to them. The `PG_BIND_ADDR` / `GRAFANA_BIND_ADDR` knobs in `.env.example` are there for `0.0.0.0` if you ever need cross-host access (you almost never do).
- **Grafana stays private.** Grafana is never exposed publicly. `GRAFANA_AUTH_ANONYMOUS_ENABLED` is off by default and not needed — the public page is generated from Postgres, not from Grafana.
- **Public page contents.** Only the generated `index.html` (baked occupancy aggregates) and the bundled `echarts.min.js` are publicly visible. No SQL, no datasource credentials, no Grafana instance, no raw per-reading database is exposed — the page ships pre-aggregated weekly/hourly numbers only.
- **`.env`.** Never commit. `rsync.sh`'s `CODE_EXCLUDES` block specifically excludes it.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `docker compose up` says "Cannot connect to the Docker daemon" | `systemctl --user start docker`, check `DOCKER_HOST` env is set |
| Container exits with "permission denied" on bind mount | Subuid/subgid not registered — re-run `subuid-register`, log out, log back in |
| `pg_isready` healthcheck never goes healthy | First-time init can take 30-60s on slow disks; if it stays unhealthy, check `docker logs gym-postgres` for SQL errors in init scripts |
| Public page panels are blank but no JS error | The relevant query returned no rows — e.g. `Popular Times` on a weekday with no history yet. Grafana shows the same gap. Resolves as data accumulates. |
| Public page charts don't render at all / blank white | `echarts.min.js` missing from the output dir — re-run `render_public_html.py` (it copies the bundle), confirm `ls ~/public_html/gym` shows both files |
| Public URL returns 403 | `chmod 711 ~ && chmod 755 ~/public_html` and verify nginx UserDir is enabled on this host |
| Public page data looks stale | The render cron has not run since the data changed. The page bakes data at render time; trigger `tools/render_public_html.py` by hand to force a refresh (see Step 7d). |
| Disk filling under `/tmp2` | Old `gym_counts.sqlite3.bak-*` files from the Mac's rsync — those land here only if you pushed code from local. Safe to `rm`. |
| `subprocess.CalledProcessError: ... psql: error: invalid percent-encoded token` | Your `.env` password has reserved URL chars; `tools/sync_local.sh` handles encoding, so confirm cron actually calls the wrapper (not raw `sync_to_postgres.py`) |
