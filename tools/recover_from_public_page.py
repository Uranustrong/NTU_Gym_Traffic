#!/usr/bin/env python3
"""Recover raw occupancy readings out of an archived public dashboard page.

Why this exists: the ws7 collector wrote into `/tmp2`, which is machine-local
scratch and was wiped around 2026-08-08 03:29. The last local `rsync.sh
pull-data` snapshot only reaches 2026-06-21, so everything collected after
that date was lost with the scratch directory.

One artifact survived: `~/htdocs/gym/index.html` lives in the NFS home, not in
`/tmp2`. `render_public_html.py` bakes its query results into the page as a
JSON blob, and its panel-3 frame is *raw* readings, not an aggregate --
`sql_panel3()` selects `(fetched_at, venue, current_count)` over a trailing
`--days` window. That gives back the final week at full 5-minute resolution.

What cannot be recovered:
  * `source_updated_at` -- the page never carried it. Rows produced here leave
    it NULL, which doubles as the marker for reconstructed data: every row that
    came from the real collector has it populated.
  * anything outside panel 3's trailing window (2026-06-22 .. 2026-07-31).

`comfortable_count` / `capacity_count` come from panel 4's `summary` rows,
which report the most recent non-null values per venue. They are effectively
constant per venue, so applying them across the window is sound.

Usage:
    python3 tools/recover_from_public_page.py \\
        --page docs/recovery/gym-page-2026-08-08.html \\
        --csv docs/recovery/recovered.csv \\
        --sqlite gym_counts.sqlite3
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from render_public_html import VENUE_DISPLAY

TAIPEI = dt.timezone(dt.timedelta(hours=8))

# The page stores display names; the DB stores the original Chinese ones.
# Invert the single source of truth rather than restating it here.
DISPLAY_TO_VENUE = {display: venue for venue, display in VENUE_DISPLAY.items()}

OCCUPANCY_COLUMNS = ("fetched_at", "source_updated_at", "venue",
                     "current_count", "comfortable_count", "capacity_count")


def extract_data_blob(html: str) -> dict:
    """Pull the `const DATA = {...};` object out of the rendered page.

    A regex cannot do this: the blob is one line, megabytes wide, and contains
    braces inside string literals (venue names, slot labels). Walk it instead,
    tracking string state so braces inside quotes do not move the depth.
    """
    marker = html.find("const DATA = ")
    if marker == -1:
        raise ValueError("no `const DATA = ` marker; is this a rendered gym page?")

    start = html.index("{", marker)
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start:index + 1])

    raise ValueError("unterminated DATA object")


def frame_rows(frame: dict) -> list[dict]:
    """Flatten a Grafana-style columnar frame into a list of row dicts."""
    names = [field["name"] for field in frame["fields"]]
    columns = [field["values"] for field in frame["fields"]]
    return [dict(zip(names, values)) for values in zip(*columns)]


def venue_capacities(data: dict) -> dict[str, tuple[int | None, int | None]]:
    """Map display name -> (comfortable_count, capacity_count) from panel 4."""
    capacities: dict[str, tuple[int | None, int | None]] = {}
    for row in frame_rows(data["panel4"]):
        if row.get("kind") != "summary":
            continue
        comfortable = row.get("comfortable")
        capacity = row.get("capacity")
        capacities[row["venue"]] = (
            int(comfortable) if comfortable is not None else None,
            int(capacity) if capacity is not None else None,
        )
    return capacities


def recover_rows(data: dict) -> list[tuple]:
    """Rebuild `occupancy` rows from panel 3, newest-last."""
    capacities = venue_capacities(data)
    rows: list[tuple] = []

    for display, frame in data["panel3"].items():
        venue = DISPLAY_TO_VENUE.get(display)
        if venue is None:
            raise ValueError(
                f"unknown display name {display!r}; add it to VENUE_DISPLAY in "
                "tools/render_public_html.py before recovering"
            )
        comfortable, capacity = capacities.get(display, (None, None))
        for row in frame_rows(frame):
            fetched_at = dt.datetime.fromtimestamp(row["time"] / 1000, TAIPEI)
            rows.append((
                fetched_at.replace(microsecond=0).isoformat(),
                None,                       # source_updated_at: not in the page
                venue,
                int(row["value"]),
                comfortable,
                capacity,
            ))

    rows.sort()
    return rows


def summarize(rows: list[tuple]) -> None:
    print(f"recovered {len(rows)} rows")
    print(f"  range   {rows[0][0]} -> {rows[-1][0]}")
    for venue, count in sorted(Counter(row[2] for row in rows).items()):
        print(f"  {venue}: {count}")
    print("  per date:")
    for date, count in sorted(Counter(row[0][:10] for row in rows).items()):
        weekday = dt.date.fromisoformat(date).strftime("%a")
        print(f"    {date} ({weekday}) {count:>4}")


def write_csv(rows: list[tuple], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(OCCUPANCY_COLUMNS)
        writer.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def merge_into_sqlite(rows: list[tuple], db_path: Path) -> int:
    """Insert rows that are not already present, keyed on (fetched_at, venue).

    The SQLite schema has only a non-unique index on (venue, fetched_at), so
    there is no ON CONFLICT to lean on -- re-running would otherwise duplicate
    every row. Filter explicitly instead.
    """
    conn = sqlite3.connect(db_path)
    try:
        existing = {
            (fetched_at, venue)
            for fetched_at, venue in conn.execute(
                "SELECT fetched_at, venue FROM occupancy")
        }
        fresh = [row for row in rows if (row[0], row[2]) not in existing]
        if not fresh:
            print(f"nothing to insert into {db_path} "
                  f"({len(rows)} rows already present)")
            return 0

        # Snapshot only when we are about to change something, so re-running
        # does not litter the repo root with identical .bak-* copies.
        backup = db_path.with_name(
            f"{db_path.name}.bak-{dt.datetime.now(TAIPEI):%Y%m%d%H%M%S}")
        shutil.copy2(db_path, backup)
        print(f"backed up {db_path} -> {backup.name}")

        conn.executemany(
            f"INSERT INTO occupancy ({', '.join(OCCUPANCY_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(OCCUPANCY_COLUMNS))})",
            fresh,
        )
        conn.commit()
    finally:
        conn.close()

    print(f"inserted {len(fresh)} rows into {db_path} "
          f"({len(rows) - len(fresh)} already present)")
    return len(fresh)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--page", required=True,
                        help="Archived index.html rendered by render_public_html.py.")
    parser.add_argument("--csv", default=None,
                        help="Write the recovered rows to this CSV path.")
    parser.add_argument("--sqlite", default=None,
                        help="Merge the recovered rows into this SQLite DB "
                             "(backs it up first, skips rows already present).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    html = Path(args.page).read_text(encoding="utf-8")
    data = extract_data_blob(html)
    rows = recover_rows(data)
    if not rows:
        print("no rows recovered", file=sys.stderr)
        return 1

    summarize(rows)

    if args.csv:
        write_csv(rows, Path(args.csv))
    if args.sqlite:
        merge_into_sqlite(rows, Path(args.sqlite))

    return 0


if __name__ == "__main__":
    sys.exit(main())
