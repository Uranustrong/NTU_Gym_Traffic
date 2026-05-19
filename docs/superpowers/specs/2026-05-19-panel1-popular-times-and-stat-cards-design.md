# Panel #1 Redesign: Popular Times + Live Cards

**Date:** 2026-05-19
**Status:** Approved design, pending implementation plan
**Scope:** `grafana/weekly-dashboard.json` + a single line in `CLAUDE.md`

## Context

The current Panel #1 is an ECharts horizontal diverging bar showing each venue's `difference_from_median`. It conveys "more or less crowded than typical right now?" in one short bar per venue, with cell color tracking the chosen `${palette}` variable. After a few rounds of iteration the user wants to try richer alternatives drawn from industry conventions: a Google-Maps-Popular-Times-style hourly bar chart, and a Datadog/Stripe-style stat card with sparkline.

Both alternatives were prototyped on the local compare page (`http://127.0.0.1:8123/panel1.html`) using real data; the user picked both for the live dashboard so they can decide between them over a few days of actual use.

The existing E panel (diverging bar, `current_vs_history`-driven) gets removed.

## Goals

- Replace Panel #1 with two new ECharts panels (B and D) that visually answer different questions about each venue's current state.
- B answers "how does now compare to today's typical hourly pattern?"
- D answers "what's the venue's current snapshot and today's trajectory?"
- Both panels read `${venue}` and `${palette}` from the existing template variables — no new variables.
- No new Grafana plugin needed (continue using `volkovlabs-echarts-panel` 7.2.4).

## Non-goals

- No changes to `sql/grafana_weekly_views.sql` (both new panels reuse existing views).
- No changes to `fetch_counts.py`, `sync_to_postgres.py`, or the verification script.
- No new template variables.
- `current_vs_history` view is kept on the database side even though no panel queries it directly any more; cleanup is a separate decision.
- Panel #2 (heatmap) and Panel #3 (line chart) keep their internals unchanged; their `gridPos.y` shifts down to make room.

## Layout

Top to bottom inside the dashboard's 24-column grid:

| Order | Panel id | Title | Type | `gridPos` |
|-------|----------|-------|------|-----------|
| 1 | 1 (rewritten) | 今日典型人潮 (Popular Times) | `volkovlabs-echarts-panel` | x=0 y=0 w=24 h=9 |
| 2 | 4 (new) | venue 現況 (Live Cards) | `volkovlabs-echarts-panel` | x=0 y=9 w=24 h=8 |
| 3 | 2 (unchanged) | Weekly 30-Minute Occupancy Heatmap | `volkovlabs-echarts-panel` | x=0 y=17 w=24 h=18 |
| 4 | 3 (unchanged) | Selected Venue Exact Counts | `volkovlabs-echarts-panel` | x=0 y=35 w=24 h=10 |

## Design

### Shared conventions

- Both new panels use the `${palette}` variable to colour highlight elements. They include the same SQL refresh-sentinel comment (`-- refresh on: ${palette}`) so Grafana picks up palette changes as a dependency.
- Both panels duplicate the same `PALETTES` constant block as panels #2 and #3 — there is no shared JS scope between panels in vanilla Grafana, and the constant is small. This is an accepted trade-off documented in the original spec.
- The same `context.panel.data.series` / `context.grafana.replaceVariables` plugin API used by panels #2 and #3 applies here.

### Panel #1 — Popular Times (B)

**Purpose:** Show the selected venue's typical hourly occupancy for today's weekday, with the current hour highlighted.

**SQL** (one query, returns one row per hour + one extra row for current):

```sql
-- refresh on: ${palette}
WITH today AS (
  SELECT EXTRACT(ISODOW FROM now() AT TIME ZONE 'Asia/Taipei')::int AS wd
),
hourly_typical AS (
  SELECT
    EXTRACT(HOUR FROM slot_start_time)::int AS hour,
    ROUND(AVG(avg_current_count))::int      AS typical_count,
    SUM(sample_count)                       AS samples
  FROM weekly_occupancy_slots, today
  WHERE venue = ${venue:sqlstring}
    AND weekday_number = today.wd
  GROUP BY hour
),
current_now AS (
  SELECT DISTINCT ON (venue)
    EXTRACT(HOUR FROM fetched_at AT TIME ZONE 'Asia/Taipei')::int AS hour,
    current_count
  FROM occupancy
  WHERE venue = ${venue:sqlstring}
  ORDER BY venue, fetched_at DESC
)
SELECT 'typical'::text AS kind, hour, typical_count AS count, samples
FROM hourly_typical
UNION ALL
SELECT 'now'::text, hour, current_count, 1
FROM current_now
ORDER BY kind DESC, hour;
```

The `kind` column lets the JS split the response into "background bars" (typical) and "current reading marker" (now).

**ECharts options:**

- `xAxis`: `type: 'category'`, data = list of hour strings (`'06:00'`, `'07:00'`, … `'21:00'`) derived from the typical rows
- `yAxis`: `type: 'value'`, label "人", `splitLine` `#222`
- A single bar series for the typical hourly counts:
  - Each data item is rendered with `itemStyle.color`:
    - `#3d556a` (palette-neutral dim slate) for non-current hours
    - `PALETTES[palette][5]` (darkest stop of chosen palette) for the current hour
- A scatter series with one point at `[currentHour, currentCount]` showing the "現在: N 人" label above the bar; symbol size 0 so the dot is invisible — only the floating label is visible.
- Tooltip per typical bar: weekday name + "HH:00 平均 N 人 (K 週樣本)"
- Title at top of panel content (via `graphic.text`): "今日 (Tue) · 健身中心" — weekday short name comes from JS Intl, venue name from `${venue}`.

**Current-hour determination:** JS uses `new Intl.DateTimeFormat('en', {hour:'numeric', hour12:false, timeZone:'Asia/Taipei'}).format(new Date())`. Grafana's dashboard refresh interval (5 min) ensures the highlight migrates forward through the day.

### Panel #4 — Live Cards (D)

**Purpose:** A two-up card layout showing each venue's current count, capacity ratio, delta vs the same-slot historical median, and a sparkline of today's actual readings.

**SQL** (one query returns summary rows + reading rows for both venues):

```sql
-- refresh on: ${palette}
WITH today_start AS (
  SELECT (date_trunc('day', now() AT TIME ZONE 'Asia/Taipei') AT TIME ZONE 'Asia/Taipei') AS ts
),
latest_meta AS (
  SELECT DISTINCT ON (venue)
    venue, comfortable_count, capacity_count
  FROM occupancy
  WHERE comfortable_count IS NOT NULL OR capacity_count IS NOT NULL
  ORDER BY venue, fetched_at DESC
),
readings_today AS (
  SELECT venue, fetched_at, current_count
  FROM occupancy, today_start
  WHERE fetched_at >= today_start.ts
)
SELECT
  'summary'::text AS kind,
  cvh.venue,
  EXTRACT(EPOCH FROM cvh.latest_fetched_at) * 1000 AS ts_ms,
  cvh.current_count,
  cvh.historical_median_current_count   AS hist_median,
  cvh.difference_from_median            AS diff,
  cvh.current_crowding_ratio            AS ratio,
  meta.comfortable_count                AS comfortable,
  meta.capacity_count                   AS capacity
FROM current_vs_history cvh
LEFT JOIN latest_meta meta ON meta.venue = cvh.venue
UNION ALL
SELECT
  'reading'::text, venue,
  EXTRACT(EPOCH FROM fetched_at) * 1000,
  current_count,
  NULL, NULL, NULL, NULL, NULL
FROM readings_today
ORDER BY kind DESC, venue, ts_ms;
```

This returns one `summary` row per venue followed by many `reading` rows per venue. JS splits and builds two card data records.

**ECharts options:**

- The panel is **one ECharts instance with two `grid` regions** side by side:
  - Left grid: x=2% to x=48% (one venue's sparkline)
  - Right grid: x=52% to x=98% (other venue's sparkline)
- Each grid has its own `xAxis` (type `time`, hidden) and `yAxis` (type `value`, hidden, `min: 0`).
- Two `line` series, one per venue, each attached to its grid via `gridIndex: 0 | 1`.
- `graphic.elements` overlays text on top of each grid area at absolute pixel offsets within the panel:
  - Venue name: 12px, `#9aa`
  - Big current count: 48px, light, colour computed from `crowding_ratio` via the same `interpStops`/luma helpers used by the heatmap (darker tint for higher crowding)
  - Subtitle: "舒適 X% · 容量 Y% (current/comfortable/capacity)", 11px, `#888`
  - Delta row: `↑/↓ N vs 同時段中位數`, 12px. Arrow + magnitude coloured by `PALETTES[palette][3]` if diff > 0 (warm side), `PALETTES[palette][1]` if diff < 0 (cool side), `#888` if diff = 0.

**Sparkline detail:**

- Line colour `PALETTES[palette][3]` (mid-dark)
- `showSymbol: false` along the line; one trailing white circle (8px) on the latest point as a `markPoint`
- Reuse the same overnight gap-padding logic from panel #3 (0-pad + null break between gaps > 1 h) so the sparkline doesn't bridge weird discontinuities if the panel is viewed late at night spanning a previous day's tail (rare, but consistent).
- No area fill — keeps the card visually quiet.

**No-data edge cases:**

- If `readings_today` is empty for a venue (e.g. dashboard opened just after midnight before the first cron tick), the sparkline series is empty and only the text overlay renders.
- If a venue has no `comfortable_count` / `capacity_count` in the data, the subtitle drops to "—". (This shouldn't happen for the two real venues but the JS handles it gracefully.)

## File changes

| File | Change |
| ---- | ------ |
| `grafana/weekly-dashboard.json` | Rewrite panel id 1 from diverging-bar to Popular Times (new SQL + new `options.getOption`). Add new panel id 4 (Live Cards). Update `gridPos.y` on panels 2 and 3 to shift them down. |
| `CLAUDE.md` | Append one bullet under "Architecture notes" listing the four-panel layout: (1) Popular Times, (4) Live Cards, (2) Heatmap, (3) Line. |

No other files touched.

## Verification

After implementation:

1. **Programmatic state check** — confirm panels 1 and 4 are `volkovlabs-echarts-panel` and their `options.getOption` strings contain the expected constants (`PALETTES`, `'typical'` / `'reading'` UNION shapes).
2. **`tools/verify_dashboard.py` passes** — no console errors, no in-DOM panel error overlays, full-page screenshot saved.
3. **Visual matrix** — refresh dashboard and walk:
   - `palette` ∈ {6 palettes}: B's current-hour bar and D's big-number colour both shift; both follow the chosen palette.
   - `venue`: B re-queries and shows the new venue's hourly profile; D's two cards do NOT change (both always shown).
   - Wait through an hour rollover (or override server time): B's highlight moves forward by one bar.
4. **gridPos sanity** — panels stack correctly, no overlap, no large empty bands. Heatmap and line chart still readable.
