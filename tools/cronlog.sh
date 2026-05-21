#!/bin/sh
# Run a command and append its combined stdout + stderr to a per-day log file
# under  logs/<YYYY-MM>/<YYYY-MM-DD>-<name>.log  — keeps cron output split by
# date and grouped by month, instead of piling into one ever-growing file.
#
# Usage (from crontab):
#   cd /repo && tools/cronlog.sh <name> <command> [args...]
set -eu

if [ "$#" -lt 2 ]; then
    echo "usage: cronlog.sh <name> <command> [args...]" >&2
    exit 2
fi

name="$1"
shift

script_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
repo_dir=$(cd -- "$script_dir/.." && pwd -P)

log_dir="$repo_dir/logs/$(date +%Y-%m)"
log_file="$log_dir/$(date +%Y-%m-%d)-$name.log"
mkdir -p "$log_dir"

# Redirect this shell — and so the exec'd command — into the day's log file.
exec >>"$log_file" 2>&1
printf '\n=== %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
exec "$@"
