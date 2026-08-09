# Trashed files

```
backups/occupancy-2026-08-09.dump.zerobyte - moved to TRASH/backups/ - zero-byte file left behind when pg_dump failed on "permission denied for sequence occupancy_id_seq". Kept briefly as evidence for the fix in 003; a failed dump that still leaves a file is the reason the backup workflow must check size and row count rather than exit code alone.
backups/occupancy-2026-08-09.dump.truncated - moved to TRASH/backups/ - 1.5K partial dump from the second failure, "query would be affected by row-level security policy". More dangerous than the zero-byte one: it looks like a valid backup.
gym_counts.sqlite3.bak-20260809113255 - moved to TRASH/ - redundant snapshot from a no-op idempotency re-run of tools/recover_from_public_page.py; identical to gym_counts.sqlite3.bak-20260809113248 taken moments earlier. The tool now only snapshots when it is actually about to insert.
sql/init/01_schema.sql - moved to TRASH/sql/ - superseded by sql/migrations/001_occupancy.sql, which docker-compose.yml now mounts into docker-entrypoint-initdb.d directly. Keeping both meant two copies of the schema that had to be edited in lockstep, exactly the drift CLAUDE.md warns about.
```
.github/workflows/schedule-probe.yml - moved to TRASH/ - temporary experiment to tell a lagging scheduler from an ignored timezone; answered by collect firing at Taipei 15:13, so the probe is redundant and would only add noise runs
.playwright-mcp/ - moved to TRASH/playwright-mcp-d4-verification - browser snapshots and console logs from verifying the D4 page; not part of the project
d4-normal.png - moved to TRASH/ - stray Playwright screenshot from verifying the D4 page
.playwright-mcp/ - moved to TRASH/playwright-mcp-d4-live - browser artefacts from verifying the page against the real Supabase endpoint
