# Data recovery — 2026-08-09

## What happened

The collector ran on `ws7.csie.ntu.edu.tw` out of `/tmp2/b12902066/Gym_Fetch`.
`/tmp2` is machine-local scratch space, and it was wiped around **2026-08-08
03:29** — the mtime of the last rendered public page, and the last moment any
part of the pipeline ran.

The local `gym_counts.sqlite3` stops at **2026-06-21T09:50+08:00**, but that is
*not* when collection stopped. Collection continued normally until
**2026-08-07T21:55+08:00**. The local snapshot is just where `rsync.sh
pull-data` was last run. Everything collected in between lived only in the
Postgres container under `/tmp2` and went with it.

Verified afterwards: `crontab -l` returns `no crontab` on both ws3 and ws7,
`/var/spool/cron/crontabs/` does not exist, and no Gym_Fetch files remain in
`$HOME`. Nothing is still running.

## What survived, and how much was lost

One artifact escaped: `~/htdocs/gym/index.html`, the static public page. It
lives in the NFS home directory, not in `/tmp2`.
[`tools/render_public_html.py`](../../tools/render_public_html.py) bakes its
query results into the page as a JSON blob, and **panel 3 is raw readings, not
an aggregate** — `sql_panel3()` selects `(fetched_at, venue, current_count)`
over a trailing 7-day window. That gave back the final week at full 5-minute
resolution.

Panel 2 carries `weekly_occupancy_slots` aggregates over *all* history, so
`sum(sample_count)` reveals exactly how large the table had grown:

| | per venue | total |
|---|---|---|
| Existed before the wipe (from panel 2 `sample_count`) | 14,900 | 29,800 |
| Present in the local SQLite snapshot | 6,510 | 13,020 |
| Recovered from the archived page (panel 3) | 1,222 | 2,444 |
| **Permanently lost (2026-06-22 → 2026-07-31)** | **7,168** | **14,336** |

Roughly 48% of the collected history is gone. This is the concrete reason the
migration plan treats backups as a cutover blocker rather than a follow-up —
see [`docs/plan/2026-08-09-github-actions-supabase-migration.md`](../plan/2026-08-09-github-actions-supabase-migration.md).

## Files here

| File | What it is |
|---|---|
| `gym-page-2026-08-08.html` | Byte-exact archive of `~/htdocs/gym/index.html` (166,203 bytes, sha256 `b21593d8…`). The **only** surviving source for the recovered week — do not delete. |
| `recovered-2026-08-01_to_08-07.csv` | The 2,444 reconstructed rows, in `occupancy` column order. |

Reproduce either from the archive:

```bash
python3 tools/recover_from_public_page.py \
    --page docs/recovery/gym-page-2026-08-08.html \
    --csv docs/recovery/recovered-2026-08-01_to_08-07.csv \
    --sqlite gym_counts.sqlite3
```

The `--sqlite` merge is idempotent: it filters on `(fetched_at, venue)` and only
snapshots the DB when it is actually going to insert. The SQLite schema has no
unique index, so that filter is doing real work — re-running without it would
duplicate every row.

## How to tell reconstructed rows apart

**Reconstructed rows have `source_updated_at IS NULL`.** The public page never
carried that column, and every row written by the real collector has it
populated (verified: 13,020 originals, 13,020 non-null). So the marker is exact
and needed no schema change:

```sql
SELECT count(*) FROM occupancy WHERE source_updated_at IS NULL;   -- 2444
```

Current local state after the merge:

| kind | rows | first | last |
|---|---|---|---|
| original | 13,020 | 2026-05-14T14:02:47+08:00 | 2026-06-21T09:50:00+08:00 |
| reconstructed | 2,444 | 2026-08-01T09:00:00+08:00 | 2026-08-07T21:55:00+08:00 |

Per-day counts of the recovered week match the expected cadence exactly —
weekday 192/venue, Saturday 156, Sunday 108 — except 2026-08-05, which is short
by 2 readings. The old ws7 cron was delivering essentially 100%, which is a
useful baseline when judging GitHub Actions' scheduling reliability later.

## Caveats

- `source_updated_at` is unrecoverable; it is NULL for the whole recovered week.
  Any analysis that dedupes on `(venue, source_updated_at)` must exclude these.
- `comfortable_count` / `capacity_count` come from panel 4's `summary` rows,
  which report the most recent non-null value per venue (健身中心 80/161,
  室內游泳池 50/130). They are effectively constant, so applying them backwards
  across the week is sound — but they are *inferred*, not observed per reading.
- Panel 3 filters to opening hours, so any reading the collector took outside
  them would not appear here. In practice the cron only ran during opening
  hours, so nothing is expected to be missing on that account.
- The panel 2 aggregates covering the lost period are still inside the archived
  HTML (`data["panel2"]`), but they are pooled across all history and cannot be
  separated by date. They preserve the shape of that period, not its rows.
  `extract_data_blob()` and `frame_rows()` in the recovery tool will get at them
  if that is ever needed.
