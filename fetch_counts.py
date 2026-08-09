#!/usr/bin/env python3
import argparse
import csv
import html as html_lib
import http.client
import re
import ssl
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


URL = "https://rent.pe.ntu.edu.tw/"
DEFAULT_DB = "gym_counts.sqlite3"
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAY_SECONDS = 10
DEFAULT_LOG_DIR = "logs"
DEFAULT_MAX_SOURCE_AGE_MINUTES = 15
# Tolerance for the source page's clock running ahead of ours. Readings are
# aligned to the minute at both ends, so a couple of minutes covers ordinary
# skew; beyond that the timestamp is not trustworthy as a freshness signal.
SOURCE_CLOCK_SKEW_MINUTES = 2

# Pin the timezone instead of trusting the host's. The opening-hours logic
# compares raw hour numbers, so on a UTC machine -- every GitHub Actions runner
# -- `--open-hours-only` would silently skip nearly every scheduled run and
# stamp rows with the wrong local day. Stored rows keep their explicit +08:00
# offset either way, so this changes decisions, not history.
TAIPEI = ZoneInfo("Asia/Taipei")

# The source page reports its own timestamp as naive Taipei local time.
SOURCE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M"


def now_taipei():
    return datetime.now(TAIPEI)


class DailyLogStream:
    def __init__(self, stream, log_dir):
        self.stream = stream
        self.log_dir = Path(log_dir)

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()

        if not text:
            return

        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{now_taipei():%Y-%m-%d}.log"
        with open(log_path, "a", encoding="utf-8") as file:
            file.write(text)

    def flush(self):
        self.stream.flush()


def enable_daily_logging(log_dir):
    sys.stdout = DailyLogStream(sys.stdout, log_dir)
    sys.stderr = DailyLogStream(sys.stderr, log_dir)


def make_ssl_context():
    context = ssl.create_default_context()

    # OpenSSL 3.x strict mode can reject some otherwise valid institutional
    # certificate chains, including chains missing Subject Key Identifier.
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT

    return context


def fetch_html(url):
    request = Request(
        url,
        headers={
            "User-Agent": "Gym-Fetch/1.0 (+personal occupancy history script)",
        },
    )
    with urlopen(request, timeout=20, context=make_ssl_context()) as response:
        return response.read().decode("utf-8", errors="replace")


def clean_text(value):
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def parse_counts(html):
    update_match = re.search(r"最後更新時間\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2})", clean_text(html))
    source_updated_at = update_match.group(1) if update_match else None

    section_matches = list(
        re.finditer(r'<div class="CMCItem">', html, flags=re.S)
    )

    rows = []
    for index, match in enumerate(section_matches):
        section_start = match.end()
        section_end = section_matches[index + 1].start() if index + 1 < len(section_matches) else len(html)
        item_html = html[section_start:section_end]
        venue_match = re.search(r'<div class="IT">\s*([^<]+)\s*</div>', item_html, flags=re.S)
        if not venue_match:
            continue

        values = {}
        for value, label in re.findall(
            r'<div class="ICI"><span>(\d+)</span>\s*([^<]+)\s*</div>',
            item_html,
            flags=re.S,
        ):
            values[clean_text(label)] = int(value)
        rows.append(
            {
                "venue": clean_text(venue_match.group(1)),
                "current_count": values.get("現在人數"),
                "comfortable_count": values.get("最適人數"),
                "capacity_count": values.get("最大乘載人數"),
                "source_updated_at": source_updated_at,
            }
        )

    return [row for row in rows if row["current_count"] is not None]


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS occupancy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            source_updated_at TEXT,
            venue TEXT NOT NULL,
            current_count INTEGER NOT NULL,
            comfortable_count INTEGER,
            capacity_count INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_occupancy_venue_time
        ON occupancy (venue, fetched_at)
        """
    )
    return conn


def is_open_time(value):
    weekday = value.weekday()
    hour = value.hour

    if weekday <= 4:
        return 6 <= hour < 22
    if weekday == 5:
        return 9 <= hour < 22
    return 9 <= hour < 18


def opening_hour(value):
    if value.weekday() <= 4:
        return 6
    return 9


def next_open_time(value):
    candidate = value.replace(
        hour=opening_hour(value),
        minute=0,
        second=0,
        microsecond=0,
    )
    if value < candidate:
        return candidate

    next_day = value + timedelta(days=1)
    return next_day.replace(
        hour=opening_hour(next_day),
        minute=0,
        second=0,
        microsecond=0,
    )


def next_interval_time(value, interval_seconds):
    day_start = value.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_seconds = int((value - day_start).total_seconds())
    remainder = elapsed_seconds % interval_seconds
    if remainder == 0 and value.microsecond == 0:
        return value.replace(microsecond=0)
    return day_start + timedelta(seconds=elapsed_seconds + interval_seconds - remainder)


def save_rows(conn, rows, fetched_at=None):
    if fetched_at is None:
        fetched_at = now_taipei().replace(second=0, microsecond=0)
    fetched_at_text = fetched_at.isoformat()
    conn.executemany(
        """
        INSERT INTO occupancy (
            fetched_at, source_updated_at, venue,
            current_count, comfortable_count, capacity_count
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                fetched_at_text,
                row["source_updated_at"],
                row["venue"],
                row["current_count"],
                row["comfortable_count"],
                row["capacity_count"],
            )
            for row in rows
        ],
    )
    conn.commit()
    return fetched_at_text


def export_csv(conn, output_path):
    cursor = conn.execute(
        """
        SELECT fetched_at, source_updated_at, venue,
               current_count, comfortable_count, capacity_count
        FROM occupancy
        ORDER BY fetched_at, venue
        """
    )
    with open(output_path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([description[0] for description in cursor.description])
        writer.writerows(cursor)


def parse_source_timestamp(text):
    """Read the page's own timestamp, which is naive Taipei local time."""
    if not text:
        return None
    try:
        return datetime.strptime(text, SOURCE_TIMESTAMP_FORMAT).replace(tzinfo=TAIPEI)
    except ValueError:
        return None


def validate_rows(rows, fetched_at, expect_venues=None,
                  max_source_age_minutes=DEFAULT_MAX_SOURCE_AGE_MINUTES):
    """Return human-readable problems with a scraped batch; empty means good.

    parse_counts() only guarantees "at least one row parsed". A page that
    renders just one of the two venues, or one whose timestamp has frozen,
    would otherwise produce a green run and a quietly incomplete table --
    the kind of gap that is invisible until someone plots it months later.
    """
    problems = []

    seen = set()
    duplicates = {row["venue"] for row in rows
                  if row["venue"] in seen or seen.add(row["venue"])}
    if duplicates:
        # One venue twice means the page markup changed shape; the unique index
        # on (fetched_at, venue) would silently keep only one of them.
        problems.append(f"duplicate venue(s): {', '.join(sorted(duplicates))}")

    if expect_venues:
        found = {row["venue"] for row in rows}
        missing = expect_venues - found
        unexpected = found - expect_venues
        if missing:
            problems.append(f"missing venue(s): {', '.join(sorted(missing))}")
        if unexpected:
            problems.append(f"unexpected venue(s): {', '.join(sorted(unexpected))}")

    for row in rows:
        current = row["current_count"]
        capacity = row["capacity_count"]
        if current < 0:
            problems.append(f'{row["venue"]}: negative current_count {current}')
        elif capacity and current > capacity * 2:
            # Genuine crowding overshoots the comfortable figure and can nudge
            # past capacity; double capacity is a parse error, not a crowd.
            problems.append(
                f'{row["venue"]}: current_count {current} far exceeds '
                f'capacity {capacity}')

    if max_source_age_minutes is not None:
        # The timestamp is page-global, so check each distinct value once
        # rather than emitting the same complaint per venue.
        for stamp in {row["source_updated_at"] for row in rows}:
            source_time = parse_source_timestamp(stamp)
            if source_time is None:
                problems.append(f"unparseable source timestamp {stamp!r}")
                continue
            age_minutes = (fetched_at - source_time).total_seconds() / 60
            if age_minutes > max_source_age_minutes:
                problems.append(
                    f"source timestamp {stamp} is {age_minutes:.0f} min old "
                    f"(limit {max_source_age_minutes})")
            elif age_minutes < -SOURCE_CLOCK_SKEW_MINUTES:
                # A timestamp from the future means the page's clock and ours
                # disagree badly enough that "fresh" is not a claim we can make.
                problems.append(
                    f"source timestamp {stamp} is {-age_minutes:.0f} min in "
                    f"the future")

    return problems


def fetch_and_save(conn, url, fetched_at=None, exit_on_error=True, retries=DEFAULT_RETRIES,
                   expect_venues=None,
                   max_source_age_minutes=DEFAULT_MAX_SOURCE_AGE_MINUTES):
    rows = []

    for attempt in range(retries + 1):
        if attempt:
            print(f"retrying fetch ({attempt}/{retries})", flush=True)
            time.sleep(DEFAULT_RETRY_DELAY_SECONDS)

        try:
            html = fetch_html(url)
        except (OSError, http.client.HTTPException) as error:
            # URLError and TimeoutError are both OSError subclasses. Catching
            # only URLError meant a read timeout or a dropped connection
            # escaped as a traceback instead of being retried -- rare on the
            # campus network, much less so over the public internet.
            print(f"fetch failed: {error!r}", file=sys.stderr)
            rows = []
            continue

        rows = parse_counts(html)
        if not rows:
            print("no occupancy rows found", file=sys.stderr)
            continue

        problems = validate_rows(
            rows, fetched_at or now_taipei(), expect_venues, max_source_age_minutes)
        if not problems:
            break

        for problem in problems:
            print(f"rejected batch: {problem}", file=sys.stderr)
        rows = []

    if not rows:
        if exit_on_error:
            sys.exit(1)
        return False

    saved_at = save_rows(conn, rows, fetched_at=fetched_at)
    for row in rows:
        print(
            f'{saved_at} {row["venue"]}: {row["current_count"]} '
            f'(source updated: {row["source_updated_at"] or "unknown"})',
            flush=True,
        )
    return True


def run_loop(conn, url, interval_seconds, open_hours_only,
             expect_venues=None,
             max_source_age_minutes=DEFAULT_MAX_SOURCE_AGE_MINUTES):
    while True:
        now = now_taipei()
        target = next_interval_time(now, interval_seconds)
        if open_hours_only and not is_open_time(target):
            target = next_open_time(target)

        wait_seconds = max(0, (target - now).total_seconds())
        if wait_seconds:
            print(f"waiting until {target.isoformat()}", flush=True)
            time.sleep(wait_seconds)

        fetch_and_save(
            conn, url, fetched_at=target, exit_on_error=False,
            expect_venues=expect_venues,
            max_source_age_minutes=max_source_age_minutes,
        )
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Fetch NTU gym and pool occupancy into SQLite.")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path. Default: {DEFAULT_DB}")
    parser.add_argument("--url", default=URL, help=f"Source URL. Default: {URL}")
    parser.add_argument("--csv", help="Export saved history to CSV instead of fetching.")
    parser.add_argument("--loop", action="store_true", help="Keep running and fetch on aligned interval boundaries.")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help=f"Loop interval in seconds. Default: {DEFAULT_INTERVAL_SECONDS}.")
    parser.add_argument("--open-hours-only", action="store_true", help="Only fetch during sports center opening hours.")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help=f"Directory for daily log files. Default: {DEFAULT_LOG_DIR}.")
    parser.add_argument("--no-log-file", action="store_true", help="Print to the terminal only; do not write daily log files.")
    parser.add_argument("--expect-venues", default=None, help="Comma-separated venue names that must all be present, with nothing else. Example: 健身中心,室內游泳池")
    parser.add_argument("--max-source-age-minutes", type=int, default=DEFAULT_MAX_SOURCE_AGE_MINUTES, help=f"Reject a batch whose source timestamp is older than this. Default: {DEFAULT_MAX_SOURCE_AGE_MINUTES}.")
    parser.add_argument("--allow-stale-source", action="store_true", help="Skip the source-timestamp freshness check. Needed for smoke tests outside opening hours, when the page legitimately stops updating.")
    args = parser.parse_args()

    expect_venues = None
    if args.expect_venues:
        expect_venues = {name.strip() for name in args.expect_venues.split(",") if name.strip()}
    max_source_age = None if args.allow_stale_source else args.max_source_age_minutes

    db_path = Path(args.db)
    conn = connect(db_path)

    if args.csv:
        export_csv(conn, args.csv)
        print(f"exported {args.csv}")
        return

    if not args.no_log_file:
        enable_daily_logging(args.log_dir)

    if args.loop:
        run_loop(
            conn, args.url, args.interval, args.open_hours_only,
            expect_venues=expect_venues,
            max_source_age_minutes=max_source_age,
        )
        return

    now = now_taipei().replace(second=0, microsecond=0)
    if args.open_hours_only and not is_open_time(now):
        print(f"{now.isoformat()} closed hours; skipped")
        return

    fetch_and_save(
        conn, args.url, fetched_at=now,
        expect_venues=expect_venues,
        max_source_age_minutes=max_source_age,
    )


if __name__ == "__main__":
    main()
