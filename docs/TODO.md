# Feature TODO / Backlog

Deferred features and undecided design calls — intentionally not done yet, with
enough context to pick them up later. Move an item out of this file when it
ships.

---

## Panel week navigation — prev / next week arrows

**Idea:** ◀ / ▶ arrows beside a panel to step through one week at a time,
instead of a single fixed window.

**Status:** deferred — collection started 2026-05-14, so there is only ~1 week
of data and nothing to navigate yet. Revisit once several weeks accumulate.

**Constraints / notes:**
- The public page (`tools/render_public_html.py` → `~/htdocs/gym/index.html`)
  is **static**. Existing interactivity (palette / venue / granularity) works
  only because every option's data is baked into the HTML and JS switches
  between baked sets. Week navigation needs the same: bake every week's data,
  plus hand-written arrow controls.
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

**Status:** undecided. Options discussed:
- (A) break the line at same-day gaps > 30 min by inserting a `null` point —
  same idea as panel 4's `padReadings` helper;
- (B) leave it as-is;
- (C) fill the gap with a historical estimate — weak: too little history, and
  no prior same-weekday data exists yet.

**Notes:** the only real gap so far (2026-05-20 14:30–16:50, a server-migration
artifact) leaves the `--days 7` window on its own after a week.
