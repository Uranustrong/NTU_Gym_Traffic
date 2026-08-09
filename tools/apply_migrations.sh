#!/usr/bin/env bash
# Apply sql/migrations/*.sql in filename order with an owner-level credential.
#
# Schema, roles, grants and RLS all live in version control rather than in the
# Supabase SQL Editor, so a restore drill can rebuild the database from an
# empty one and every change is reviewable in a diff.
#
# Usage:
#   export PGPASSWORD='<owner password>'
#   export GYM_WRITER_PASSWORD='...' GYM_READER_PASSWORD='...'
#   tools/apply_migrations.sh 'postgresql://postgres.<ref>@<host>:5432/postgres'
#
# Every file is idempotent, so re-running is safe and is how you roll a role
# password. Pass --skip-roles to apply only the schema and views, which is what
# you want for a throwaway test database that has no workflow roles.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIGRATIONS_DIR="$REPO_DIR/sql/migrations"

SKIP_ROLES=0
POSTGRES_URL=""
for arg in "$@"; do
    case "$arg" in
        --skip-roles) SKIP_ROLES=1 ;;
        -h|--help)
            sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *)  POSTGRES_URL="$arg" ;;
    esac
done

if [ -z "$POSTGRES_URL" ]; then
    echo "usage: $0 [--skip-roles] <postgres-url>" >&2
    exit 2
fi

# Fail before touching the database rather than half way through, and never
# invent a default password.
if [ "$SKIP_ROLES" -eq 0 ]; then
    missing=""
    for var in GYM_WRITER_PASSWORD GYM_READER_PASSWORD; do
        [ -n "${!var:-}" ] || missing="$missing $var"
    done
    if [ -n "$missing" ]; then
        echo "error: required environment variable(s) unset:$missing" >&2
        echo "       (or pass --skip-roles to apply schema and views only)" >&2
        exit 2
    fi
fi

for file in "$MIGRATIONS_DIR"/*.sql; do
    name="$(basename "$file")"
    case "$name" in
        003_*|004_*)
            if [ "$SKIP_ROLES" -eq 1 ]; then
                echo "--> skipping $name (--skip-roles)"
                continue
            fi
            ;;
    esac

    echo "==> $name"
    # Role passwords are NOT passed as -v: that would put them in the psql
    # command line for anyone running `ps`. 003 reads them from the inherited
    # environment with \getenv instead.
    #
    # --single-transaction so a file is all-or-nothing. Without it, 004 could
    # drop the old policies, fail while creating the new ones, and leave the
    # table with RLS enabled and no policy at all -- locking out the very
    # roles it is supposed to admit.
    psql "$POSTGRES_URL" \
        -v ON_ERROR_STOP=1 \
        --single-transaction \
        --quiet --no-psqlrc \
        -f "$file"
done

echo "all migrations applied"
