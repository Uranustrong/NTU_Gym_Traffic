#!/usr/bin/env bash
set -euo pipefail

REMOTE="${REMOTE:-b12902066@ws7.csie.ntu.edu.tw}"
REMOTE_DIR="${REMOTE_DIR:-/tmp2/b12902066/Gym_Fetch}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LOCAL_DIR="${LOCAL_DIR:-$SCRIPT_DIR}"
DB_NAME="${DB_NAME:-gym_counts.sqlite3}"
SNAPSHOT_NAME="${SNAPSHOT_NAME:-gym_counts.snapshot.sqlite3}"

# Auto-load .env if present so POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
# (or an explicit POSTGRES_URL) flow in from the same file docker-compose reads.
# We parse it line-by-line instead of `source .env`, because passwords often
# contain characters that bash would interpret (parens, $, etc.).
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"  # ltrim
    key="${key%"${key##*[![:space:]]}"}"  # rtrim
    # Strip optional surrounding quotes
    if [[ "$value" =~ ^\".*\"$ ]] || [[ "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$SCRIPT_DIR/.env"
fi

POSTGRES_USER="${POSTGRES_USER:-songhejun}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-gym_fetch_dev}"
POSTGRES_DB="${POSTGRES_DB:-gym_fetch}"

# Construct POSTGRES_URL if not explicitly set. Password is URL-encoded via
# python3 stdlib so passwords with special chars (`%`, `:`, `@`, `(`, etc.)
# round-trip correctly through libpq's connection URI parser.
if [[ -z "${POSTGRES_URL:-}" ]]; then
  ENC_USER=$(python3 -c "import sys, urllib.parse as u; print(u.quote(sys.argv[1], safe=''))" "$POSTGRES_USER")
  ENC_PASS=$(python3 -c "import sys, urllib.parse as u; print(u.quote(sys.argv[1], safe=''))" "$POSTGRES_PASSWORD")
  POSTGRES_URL="postgresql://${ENC_USER}:${ENC_PASS}@127.0.0.1:5433/${POSTGRES_DB}"
fi

MODE="${1:-pull-data}"

CODE_EXCLUDES=(
  "--exclude=.git/"
  "--exclude=.agents/"
  "--exclude=.codex/"
  "--exclude=.venv/"
  "--exclude=__pycache__/"
  "--exclude=*.pyc"
  "--exclude=*.sqlite3"
  "--exclude=*.csv"
  "--exclude=*.log"
  "--exclude=logs/"
)

pull_data() {
  mkdir -p "$LOCAL_DIR/logs"

  ssh "$REMOTE" "cd '$REMOTE_DIR' && sqlite3 '$DB_NAME' \".backup '$SNAPSHOT_NAME'\""
  rsync -avhz --backup --suffix=".bak-$(date +%Y%m%d%H%M%S)" \
    "$REMOTE:$REMOTE_DIR/$SNAPSHOT_NAME" "$LOCAL_DIR/$DB_NAME"
  rsync -avhz "$REMOTE:$REMOTE_DIR/logs/" "$LOCAL_DIR/logs/"

  if [[ -f "$LOCAL_DIR/sync_to_postgres.py" ]]; then
    python3 "$LOCAL_DIR/sync_to_postgres.py" \
      --sqlite-db "$LOCAL_DIR/$DB_NAME" \
      --postgres-url "$POSTGRES_URL"
  fi
}

push_code() {
  rsync -avhz "${CODE_EXCLUDES[@]}" "$LOCAL_DIR/" "$REMOTE:$REMOTE_DIR/"
}

pull_code() {
  rsync -avhz "${CODE_EXCLUDES[@]}" "$REMOTE:$REMOTE_DIR/" "$LOCAL_DIR/"
}

case "$MODE" in
  pull-data)
    pull_data
    ;;
  push-code)
    push_code
    ;;
  pull-code)
    pull_code
    ;;
  *)
    echo "Usage: $0 [pull-data|push-code|pull-code]" >&2
    exit 2
    ;;
esac
