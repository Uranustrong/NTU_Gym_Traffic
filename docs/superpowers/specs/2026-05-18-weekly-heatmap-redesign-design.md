# Weekly Dashboard Heatmap Redesign

**Date:** 2026-05-18
**Status:** Approved design, pending implementation plan
**Scope:** `grafana/weekly-dashboard.json` + README/CLAUDE.md updates + one Grafana plugin

## Context

The weekly dashboard's heatmap panel (`Weekly 30-Minute Occupancy Heatmap`) renders 28 rows × 7 columns of 30-minute slots in a Grafana `table` panel with `color-background` cells. Two user-facing problems:

1. **Vertical layout** — 28 rows don't fit on one screen; users must scroll.
2. **Default colors** — the 3-step `green/yellow/red @ 0/40/80` thresholds look harsh and don't match the rest of the dashboard's intended aesthetic.

The two sibling panels (`Current vs Same Weekday/Time History` table and `Selected Venue Exact Counts` timeseries) work correctly but use Grafana's default colors and don't visually cohere with a redesigned heatmap.

This spec describes a coherent visual refresh across all three panels, driven by runtime-selectable template variables.

## Goals

- Heatmap fits on one screen at 1-hour granularity (the default). 30-minute granularity is available for users who want finer detail and accept some scrolling.
- Heatmap palette is selectable from 6 Morandi-style options at runtime, without editing the dashboard JSON.
- Color in the heatmap consistently encodes "crowdedness" (crowding_ratio) regardless of which metric the user displays in the cell.
- Panels #1 and #3 share a coordinated visual language with the heatmap.
- The redesign stays within Grafana + one mainstream plugin; no custom plugin development.

## Non-goals

- No changes to `fetch_counts.py`, `sync_to_postgres.py`, or `sql/grafana_weekly_views.sql` (views unchanged; new bucketing is per-query CTE).
- No changes to the older `Gym Occupancy` dashboard (`uid: ad854qp`).
- No changes to the dashboard's existing `venue` and `metric` template variables.
- No new automated tests for ECharts panel rendering (config-only changes; existing SQL view tests must still pass).

## Design

### Dependency

Install `volkovlabs-echarts-panel` in the `gym-grafana` container:

```bash
docker exec gym-grafana grafana cli plugins install volkovlabs-echarts-panel
docker restart gym-grafana
```

This is a signed plugin in Grafana's catalog. No `allow_loading_unsigned_plugins` setting needed.

### Template variables

Two new variables, appended to the existing `venue` and `metric`:

| name          | type   | values                                              | default   |
| ------------- | ------ | --------------------------------------------------- | --------- |
| `granularity` | custom | `30min,60min`                                       | `60min`   |
| `palette`     | custom | `terracotta,sage,slate,rose,teal,lavender`          | `slate`   |

### Panel #1 — `Current vs Same Weekday/Time History` (unchanged structure)

Stays `type: "table"`. Only one cell color override changes: the `difference_from_median` field's threshold list moves from the existing 3-step traffic light to a **static 5-step Morandi diverging palette**:

```
value ≤ -10 : #5e7689  (cool slate — "much less crowded than typical")
       -3  : #bdcad4
        0  : #ece5d6  (neutral linen — at the typical median)
        3  : #d8a78a
       ≥10 : #974a3a  (terracotta — "much more crowded than typical")
```

`cellOptions.mode: "gradient"` so the in-between values interpolate smoothly.

This palette is **independent of the `${palette}` variable** — diverging color logic is a different visual mode from the sequential palette used by the heatmap. All other columns in this panel are untouched.

### Panel #2 — `Weekly 30-Minute Occupancy Heatmap` (full rewrite)

**Panel type:** `volkovlabs-echarts-panel`. `gridPos.h` stays at 18; ECharts auto-fits.

**SQL (this panel only).** Wraps the existing `weekly_occupancy_slots` view with granularity bucketing:

```sql
WITH bucketed AS (
  SELECT
    weekday_number,
    CASE WHEN '${granularity}' = '60min'
      THEN to_char(date_trunc('hour', ('2000-01-01 '||slot_start_time)::timestamp), 'HH24:00')
      ELSE slot_label
    END AS slot_label,
    CASE WHEN '${granularity}' = '60min'
      THEN date_trunc('hour', ('2000-01-01 '||slot_start_time)::timestamp)::time
      ELSE slot_start_time
    END AS slot_start_time,
    avg_current_count,
    avg_crowding_ratio,
    sample_count
  FROM weekly_occupancy_slots
  WHERE venue = ${venue:sqlstring}
)
SELECT
  weekday_number,
  slot_label,
  slot_start_time,
  AVG(avg_current_count)   AS avg_count,
  AVG(avg_crowding_ratio)  AS crowding_ratio,
  SUM(sample_count)        AS sample_count
FROM bucketed
GROUP BY weekday_number, slot_label, slot_start_time
ORDER BY slot_start_time, weekday_number;
```

Long-table output; ECharts JS pivots into the heatmap grid client-side.

**ECharts options structure:**

- `xAxis` = 7 weekday categories (`Mon, Tue, Wed, Thu, Fri, Sat, Sun`), positioned at the top.
- `yAxis` = time-slot categories ordered ascending by `slot_start_time` with `inverse: true` (earliest at top, latest at bottom).
- `series[0]` = `heatmap`, with each data point carrying `[xIdx, yIdx, crowding_ratio_pct, label_value]`.
  - `crowding_ratio_pct` drives `visualMap` color mapping (range 0-100).
  - `label_value` is `avg_count` (rounded) when `${metric}='avg_count'`, or `crowding_ratio_pct` (rounded `%`) when `${metric}='crowding_ratio'`.
- `visualMap` = horizontal gradient bar at the bottom of the panel, range 0-100, colors from the active palette's 6 stops.
- `tooltip` shows: weekday + slot label / `avg_count` (人) / `crowding_ratio` (%) / `sample_count` (週).
- Cell label font color picks black or white per ITU-R 601 luma threshold of the cell background.
- Closed-hour cells: SQL doesn't return rows for them, so ECharts simply doesn't draw a rectangle — transparent gaps appear naturally for Saturday morning, Sunday evening, etc.

**Palette constants (inline in panel JS):**

```js
const PALETTES = {
  terracotta: ['#ede4d4','#d8c4a0','#caa479','#b8754f','#974a3a','#642822'],
  sage:       ['#e8e7d6','#c9d2b3','#9eb288','#728c64','#4f6948','#2e4530'],
  slate:      ['#e6ebef','#bdcad4','#8ea1b1','#5e7689','#3d556a','#22344a'],
  rose:       ['#ece1de','#dbb9b9','#c08e95','#9a5e72','#6d3f56','#412236'],
  teal:       ['#dde7e5','#a8c2bf','#6fa097','#487a72','#2e5953','#1a3833'],
  lavender:   ['#e7e1ec','#c5b4d3','#a08abb','#7e639d','#5a417a','#3a2855'],
};
const stops = PALETTES[context.replaceVariables('${palette}')] ?? PALETTES.slate;
```

### Panel #3 — `Selected Venue Exact Counts` (timeseries → ECharts line)

**Panel type:** `volkovlabs-echarts-panel`. SQL unchanged.

- `xAxis` = time (Grafana time range honored via `$__timeFilter`).
- `yAxis` = `current_count`.
- Single `line` series for the selected venue.
- Line color = the active palette's high-value stop (`PALETTES[${palette}][5]`).
- Area below the line filled at ~10% opacity in the same color for visual weight.
- Tooltip shows timestamp and count.
- The same `PALETTES` constant is **duplicated inline** in this panel's JS (acceptable — small constant, lives next to the panel it serves).

## File changes

| File                                  | Change                                                                                                                                                                                                                              |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `grafana/weekly-dashboard.json`       | Add `granularity` + `palette` to `templating.list`; rewrite panel #2 (`type`, `targets[0].rawSql`, new `options.getOption` JS); rewrite panel #3 (`type`, new `options.getOption` JS); change panel #1 `difference_from_median` thresholds to the diverging palette. |
| `README_Local.md`                     | Add a "Grafana plugin requirement" subsection under the existing Grafana setup section, listing the `grafana cli plugins install volkovlabs-echarts-panel && docker restart gym-grafana` one-liner.                                  |
| `CLAUDE.md`                           | Update the "Grafana dashboard is wired to a hardcoded datasource UID" architecture note: keep the UID warning, add a sentence that the dashboard now also requires `volkovlabs-echarts-panel` and exposes `${granularity}` + `${palette}` template variables. |

No new files; no changes outside the three above.

## Verification

After implementation:

1. **Plugin install survives restart.** `docker restart gym-grafana && curl -sS -u admin:admin http://127.0.0.1:3000/api/plugins | python3 -c "import json,sys; print(any(p['id']=='volkovlabs-echarts-panel' for p in json.load(sys.stdin)))"` returns `True`.
2. **SQL view tests still pass.** `python3 -m unittest test_grafana_weekly_views.py` is green (no SQL view changes; only per-query CTE bucketing).
3. **Visual smoke test in browser.** Open the dashboard, then for each combination of `granularity` ∈ {30min, 60min} × `palette` ∈ {all 6} × `metric` ∈ {avg_count, crowding_ratio}, verify:
   - Heatmap renders without scrolling at 60min granularity.
   - Cell colors change when palette changes; cell labels change when metric changes.
   - Panel #3 line color changes when palette changes.
   - Panel #1 `difference_from_median` cell color is unchanged across palette switches (intentionally static).
4. **Closed-hour gaps render correctly** — Sunday cells after 18:00 and Saturday cells before 09:00 / after 22:00 appear as transparent gaps, not as zero-valued green cells.
