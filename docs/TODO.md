# Feature TODO / Backlog

Deferred features and undecided design calls — intentionally not done yet, with
enough context to pick them up later. Move an item out of this file when it
ships.

---

## Panel week navigation — prev / next week arrows

**Idea:** ◀ / ▶ arrows beside a panel to step through one week at a time,
instead of a single fixed window.

**Status:** deferred. Collection started 2026-05-14, so the span is now wide
enough to navigate — but it is not contiguous: 2026-06-22 → 08-07 is gone with
ws7, and the recovered week is only partially detailed. Arrows that step into
an empty week need to say "no data", not render blank axes.

**Constraints / notes:**
- **The "every option must be baked in" constraint is gone.** The page used to
  ship all its data inside the HTML, so week navigation would have meant baking
  every week. It now calls `public.gym_live()` on Supabase from the browser, so
  arrows can simply ask for the week they need. What is left is the arrow
  controls plus either an extra argument on `gym_live` or a small sibling RPC
  in `sql/migrations/005_public_api.sql`.
- Grafana already does prev / next week for the **line chart** (`Selected
  Venue Exact Counts`) through its native time-range picker.
- The **heatmap** is the exception — its SQL has no time filter, so it is
  always an all-weeks average; week navigation there is new work even in
  Grafana.
- Decide first: which panel (heatmap vs line chart), and public page vs
  Grafana-only.

---

## Tooltip — show weeks of data, not just a reading count

**Idea:** the heatmap and Popular Times tooltips show `sample_count`, a count
of 5-minute readings (now labelled `based on N readings`). A more meaningful
confidence signal is the number of distinct weeks behind each cell's average.

**Status:** deferred. The misleading `N wk` label was already fixed →
`based on N readings`. Showing the true week count is the larger follow-up.

**Notes:** add a `weeks_observed` column to `weekly_occupancy_slots` in
`sql/grafana_weekly_views.sql` (count of distinct ISO weeks), re-apply the view
to Postgres, surface it through the panel SQL and tooltips. Update
`test_grafana_weekly_views.py` if it asserts the view's column set.

---

## Line chart — honest data-gap handling

**Idea:** `Selected Venue Exact Counts` uses an ordinal category x-axis, so a
real collection gap is silently bridged by a straight diagonal line that looks
like continuous data.

**Status:** undecided, but **no longer hypothetical — this is visible on the
live page right now.** The ws7 loss left a six-week hole (2026-06-22 →
2026-08-07) and a two-day one (2026-08-07 21:55 → 2026-08-09 13:35, between the
last ws7 reading and the first GitHub Actions one). The second is inside the
`--days 7` window and is currently drawn as a straight diagonal across two days
of nothing.

Options discussed:
- (A) break the line at same-day gaps > 30 min by inserting a `null` point —
  same idea as panel 4's `padReadings` helper;
- (B) leave it as-is;
- (C) fill the gap with a historical estimate — weak, and now actively wrong:
  inventing readings across a known outage is the opposite of honest.

**Notes:** the six-week hole will never leave the heatmap or Popular Times,
because those aggregate all history rather than a trailing window. The
`--days 7` line chart clears the two-day gap on 2026-08-16. Recovered rows
carry `source_updated_at IS NULL` if the fix wants to mark them.

---

## Shrink the poll payload with a delta protocol

**Idea:** `gym_live(p_days, p_since)` — given `p_since`, return only readings
newer than it, and let the browser append to what it already has.

**Status:** deliberately not done, and the reason matters more than the idea.

**Measured** (live database, 2026-08-09). `gym_live(7)` is 8.2 KB gzipped, and
**panel3 is 96 KB of its 101 KB uncompressed** — 95%. A poll therefore re-sends
a seven-day series every five minutes in order to deliver two new readings.
`gym_live(1)` is 785 bytes, which is the same shape of evidence: the window
dominates. A delta would cut the steady-state poll roughly 25×, taking a
permanently-open tab from 67 MB/month to about 3 MB.

**Why it was still not worth doing now:**
- It optimises *legitimate* traffic, and legitimate traffic is two orders of
  magnitude from the ceiling — 5 GB/month is ~76 tabs open around the clock, or
  ~360,000 ordinary visits.
- **It does nothing against abuse**, which was the actual risk: a caller simply
  omits `p_since`. That problem is solved instead by the rate limits in
  `sql/migrations/005_public_api.sql`.
- It adds a genuine class of client bugs — cache with a hole, cache older than
  `p_since`, trimming the window — to a page that had just been verified.

**Do it when** a real audience makes egress the binding constraint. The trigger
to watch is the daily call counts against
`api_private.rate_limit_policy.daily_global` (8000 for `gym_live`).

**Notes:** clamp `p_since` no earlier than `now() - 31 days`, matching the
existing `p_days` clamp, and make the client fall back to a full fetch whenever
it cannot prove its cache is contiguous.

---

## Report on RPC usage before the quota does

**Idea:** the rate limiter now knows exactly how much the public API is used —
`api_private.rate_limit_global` holds calls per bucket per day — but nothing
looks at it, so the first sign of a problem would still be a Supabase email.

**Status:** deferred, small.

**Notes:** `backup.yml` already runs weekly with a `gym_reader` credential and
already writes a summary; adding the last seven days of counts to it is the
cheapest place to put this. Store the reading rather than trusting the table to
persist: it is `UNLOGGED`, so a restart resets it to zero, and
`pg_stat_statements` can be reset out from under a reader too — record deltas,
not absolutes.
