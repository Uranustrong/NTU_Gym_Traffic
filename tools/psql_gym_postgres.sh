#!/bin/sh
set -eu

# sync_to_postgres.py calls the psql executable as:
#   psql <postgres-url> -v ON_ERROR_STOP=1 -c <sql>
# The Dockerized setup already runs psql inside the gym-postgres container,
# so the host connection URL is not needed inside this wrapper.
if [ "$#" -gt 0 ]; then
    shift
fi

exec /opt/homebrew/bin/docker exec -i gym-postgres \
    psql -U songhejun -d gym_fetch "$@"
