#!/usr/bin/env bash
# Sync gym_counts.sqlite3 into the local postgres instance.
#
# Designed for cron on the server (ws7) where fetch_counts.py and the
# postgres container run on the same host — no rsync hop in between.
# Sources .env (same parser as rsync.sh) so the postgres URL and password
# stay out of crontab.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"

if [[ -f "$REPO_DIR/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    if [[ "$value" =~ ^\".*\"$ ]] || [[ "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$REPO_DIR/.env"
fi

POSTGRES_USER="${POSTGRES_USER:-songhejun}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-gym_fetch_dev}"
POSTGRES_DB="${POSTGRES_DB:-gym_fetch}"

if [[ -z "${POSTGRES_URL:-}" ]]; then
  ENC_USER=$(python3 -c "import sys, urllib.parse as u; print(u.quote(sys.argv[1], safe=''))" "$POSTGRES_USER")
  ENC_PASS=$(python3 -c "import sys, urllib.parse as u; print(u.quote(sys.argv[1], safe=''))" "$POSTGRES_PASSWORD")
  POSTGRES_URL="postgresql://${ENC_USER}:${ENC_PASS}@127.0.0.1:5433/${POSTGRES_DB}"
fi

# If host has no psql, wrap through the docker container.
PSQL_FLAG=()
if ! command -v psql >/dev/null 2>&1; then
  PSQL_FLAG=(--psql "$REPO_DIR/tools/psql_gym_postgres.sh")
fi

cd "$REPO_DIR"
exec python3 sync_to_postgres.py \
  --sqlite-db gym_counts.sqlite3 \
  --postgres-url "$POSTGRES_URL" \
  "${PSQL_FLAG[@]}"
