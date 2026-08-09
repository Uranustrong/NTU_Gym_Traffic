# Trashed files

```
gym_counts.sqlite3.bak-20260809113255 - moved to TRASH/ - redundant snapshot from a no-op idempotency re-run of tools/recover_from_public_page.py; identical to gym_counts.sqlite3.bak-20260809113248 taken moments earlier. The tool now only snapshots when it is actually about to insert.
sql/init/01_schema.sql - moved to TRASH/sql/ - superseded by sql/migrations/001_occupancy.sql, which docker-compose.yml now mounts into docker-entrypoint-initdb.d directly. Keeping both meant two copies of the schema that had to be edited in lockstep, exactly the drift CLAUDE.md warns about.
```
