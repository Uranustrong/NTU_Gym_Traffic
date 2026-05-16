# Grafana Weekly Display Design

## Purpose

The dashboard should help users understand when the NTU sports center is usually busy or quiet during a week. The first version focuses on intuitive historical display, not recommendations.

The main visual language is a weekly view: weekday by open-time slot. A secondary line chart shows exact counts so users can inspect numbers behind the color patterns.

## Current Context

The project already collects occupancy snapshots from `https://rent.pe.ntu.edu.tw/` into SQLite and syncs them to PostgreSQL for Grafana.

The PostgreSQL-facing table is `occupancy`:

- `fetched_at`: timestamp of the local fetch
- `source_updated_at`: timestamp shown by the source site, when present
- `venue`: sports center area
- `current_count`: observed current occupancy
- `comfortable_count`: source-provided comfortable occupancy
- `capacity_count`: source-provided maximum capacity

Rows are collected about every 5 minutes during opening hours.

## Dashboard Structure

The dashboard uses a weekly-display-first structure.

### 1. Current vs Historical Summary

The top row shows current status by venue. Each venue should display:

- latest `current_count`
- the latest data timestamp
- historical median for the same weekday and 30-minute slot
- difference between current count and that historical median

This row should not label a time as recommended. It only tells users whether the current state is above, below, or close to historical behavior.

### 2. Weekly Heatmap Main Area

The main area shows occupancy by:

- X axis: weekday
- Y axis: open-time slot
- slot size: 30 minutes
- default metric: average `current_count`

The heatmap should support:

- venue filtering
- switching between average count and crowding ratio
- sample count visibility or filtering, so sparse slots are not overread
- closed-hour cells shown as absent or neutral, not as low occupancy

Crowding ratio is calculated from `current_count / comfortable_count` when available, otherwise `current_count / capacity_count`. Ratio is useful for comparing venues with different capacities, while average count remains the default because it is easier to understand.

If Grafana's heatmap panel cannot cleanly express weekday by 30-minute slot, the first implementation should use a table panel with colored cell backgrounds. A readable weekday-slot grid is more important than using a specific Grafana visualization type.

### 3. Line Chart Detail Area

The lower area shows the selected venue's actual `current_count` over the active Grafana time range.

This chart is secondary. It supports the heatmap by showing exact numbers and short-term changes that a color grid cannot express.

## Data Views

Keep the raw `occupancy` table unchanged. Add PostgreSQL views for Grafana and document the panel SQL that reads from those views.

### weekly_occupancy_slots

Groups historical rows by venue, weekday, and 30-minute slot in Asia/Taipei time.

Expected output:

- `venue`
- `weekday_number`
- `weekday_label`
- `slot_start_time`
- `slot_label`
- `avg_current_count`
- `median_current_count`
- `min_current_count`
- `max_current_count`
- `sample_count`
- `avg_crowding_ratio`

This dataset powers the weekly heatmap and supports switching between `avg_current_count` and `avg_crowding_ratio`.

### current_vs_history

Compares each venue's latest row with historical rows from the same weekday and 30-minute slot. The latest row itself is excluded from the historical aggregate so the panel does not compare a point with itself.

Expected output:

- `venue`
- `latest_fetched_at`
- `current_count`
- `historical_median_current_count`
- `historical_avg_current_count`
- `difference_from_median`
- `sample_count`
- `current_crowding_ratio`

This dataset powers the top summary row.

## Time Rules

All grouping and labels should use Asia/Taipei local time.

Opening hours:

- Monday-Friday: 08:00-22:00
- Saturday: 09:00-22:00
- Sunday: 09:00-18:00

The weekly grid should only show open-time slots. Sunday evening and early weekday morning should not appear as low-occupancy cells.

## Grafana Variables

The first version should provide these variables where Grafana supports them cleanly:

- `venue`: selected venue, with all venues available
- `metric`: average count or crowding ratio

The default view should prioritize average count for readability.

## Error Handling and Edge Cases

Sparse data should be visible. A slot with too few samples should be marked with sample count or filtered out, instead of being presented as a reliable pattern.

Rows with missing `comfortable_count` and `capacity_count` can still contribute to average count, but they should not contribute to crowding ratio.

Duplicate fetch rows are already handled during SQLite-to-PostgreSQL sync by `(fetched_at, venue)`. The dashboard views should assume the PostgreSQL `occupancy` table is the source of truth.

## Testing and Verification

SQL should be tested with focused checks:

- 30-minute slot calculation is correct for representative timestamps.
- Weekday labels match Asia/Taipei time.
- Opening-hour filtering excludes closed slots.
- Average count, median count, and sample count are calculated correctly.
- Crowding ratio handles missing or zero capacity fields safely.
- Current-vs-history excludes the latest row from the historical comparison.

Grafana verification should check:

- heatmap or colored table is readable at dashboard width
- venue and metric variables affect panels correctly
- current summary shows the latest timestamp
- line chart shows exact counts over the selected time range

## Out of Scope for This Version

This version will not produce recommendation scores or tell users the best time to go. It will also not build predictive models.

Future work can add:

- recommended low-crowd slots
- confidence labels based on sample count and variance
- holiday or semester calendar awareness
- mobile-oriented dashboard layout
- direct Grafana dashboard JSON provisioning
