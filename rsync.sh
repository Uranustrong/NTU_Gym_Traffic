#!/usr/bin/env bash
set -euo pipefail

REMOTE="${REMOTE:-b12902066@ws7.csie.ntu.edu.tw}"
REMOTE_DIR="${REMOTE_DIR:-/tmp2/b12902066/Gym_Fetch}"
LOCAL_DIR="${LOCAL_DIR:-.}"
DB_NAME="${DB_NAME:-gym_counts.sqlite3}"
SNAPSHOT_NAME="${SNAPSHOT_NAME:-gym_counts.snapshot.sqlite3}"
POSTGRES_URL="${POSTGRES_URL:-postgresql://songhejun:gym_fetch_dev@127.0.0.1:5433/gym_fetch}"

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
