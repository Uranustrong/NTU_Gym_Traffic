#!/bin/sh
set -eu

# sync_to_postgres.py calls the psql executable as:
#   psql <postgres-url> -v ON_ERROR_STOP=1 -c <sql>
# The Dockerized setup already runs psql inside the gym-postgres container,
# so the host connection URL is not needed inside this wrapper.
if [ "$#" -gt 0 ]; then
    shift
fi

# Rootless docker keeps its API socket under the per-user runtime dir.
# Default DOCKER_HOST so this wrapper works from a minimal cron environment
# where DOCKER_HOST is not exported. An explicit DOCKER_HOST still wins.
: "${DOCKER_HOST:=unix:///run/user/$(id -u)/docker.sock}"
export DOCKER_HOST

DOCKER_BIN="${DOCKER_BIN:-docker}"
PG_USER="${POSTGRES_USER:-songhejun}"
PG_DB="${POSTGRES_DB:-gym_fetch}"

exec "$DOCKER_BIN" exec -i gym-postgres psql -U "$PG_USER" -d "$PG_DB" "$@"
