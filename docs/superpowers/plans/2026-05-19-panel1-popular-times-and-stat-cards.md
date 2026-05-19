# Panel #1 Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the diverging-bar Panel #1 with two new ECharts panels — a Google-Maps-style "Popular Times" hourly bar (panel id 1) and a Datadog-style "Live Cards" two-up with sparkline (panel id 4) — and shift the existing heatmap/line panels down to make room.

**Architecture:** All edits land in `grafana/weekly-dashboard.json` plus one bullet in `CLAUDE.md`. The dashboard JSON is re-imported into the running `gym-grafana` container via the documented curl call after each edit. There is no JS unit test infrastructure for panel code; verification is done with `python3 -m json.tool` (JSON validity), the curl import response (`"status":"success"`), and `tools/verify_dashboard.py` (Playwright headless render — exits 0 if no console errors / panel-error overlays). The new panels reuse the same Volkov Labs ECharts plugin v7.2.4 already installed; no plugin or template-variable changes.

**Tech Stack:** Grafana 13 + `volkovlabs-echarts-panel` 7.2.4, PostgreSQL 18 (existing `weekly_occupancy_slots` + `current_vs_history` views), Apache ECharts (multi-grid layouts, `graphic.elements` for text overlays), Python 3 stdlib + Playwright for verification.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `grafana/weekly-dashboard.json` | Single source of truth for dashboard layout, panel SQL, ECharts options. | Bump `panel 1` height + replace its SQL/JS; bump `panel 2`/`panel 3` `gridPos.y`; append new `panel 4`. |
| `CLAUDE.md` | Architecture notes & runbook. | Add one bullet under existing "Architecture notes" describing the 4-panel layout. |

No other files touched. No SQL view changes, no Python changes, no new plugins, no new template variables.

## Conventions used throughout the plan

- **Plugin API:** `context.panel.data.series` for query rows; `context.grafana.replaceVariables('${var}')` for template variables. Bare `data` or `replaceVariables(...)` will throw `ReferenceError`. (Source: panels #2 / #3 in the same file.)
- **PALETTES constant:** duplicated per-panel (no shared JS scope across Grafana panels). The six-stop arrays must match exactly across panels for consistent visual identity.
- **SQL refresh sentinel:** `-- refresh on: ${palette}` comment in `rawSql` is what makes Grafana re-execute the panel's query when `palette` changes, even though no `${palette}` substitution appears in the actual SQL.
- **JSON safety:** the dashboard JSON has all JS code as a JSON-encoded string in `options.getOption`. Newlines must remain `\n`, quotes must remain escaped. After every edit, run `python3 -m json.tool grafana/weekly-dashboard.json > /dev/null` to confirm validity before importing.
- **Re-import command** (used after every edit):
  ```bash
  curl -s -u admin:admin -H 'Content-Type: application/json' \
    -X POST --data @grafana/weekly-dashboard.json \
    http://127.0.0.1:3000/api/dashboards/db
  ```
  Response includes `"status":"success"` and a new `version` integer. Anything else means malformed JSON or wrong API call — STOP and inspect.
- **Headless verify command** (used after every edit):
  ```bash
  python3 tools/verify_dashboard.py
  ```
  Exit 0 = no console errors, no panel-error overlays. Exit 1 = failure — read its stdout for which panel errored and the error string.
- **Docker requirement:** `colima` and the `gym-grafana` + `gym-postgres` containers must be running. If `curl` returns "Connection refused", run `colima start && docker start gym-grafana gym-postgres` and retry.
- **No-third-party-pkgs policy:** all Python in this repo uses stdlib only; Playwright is a verification-only dev dependency (installed per-user via `pip install --user playwright && python3 -m playwright install chromium`), not in `requirements.txt`. Don't add anything to `requirements.txt`.

---

## Task 1: Shift `gridPos.y` on panels 2 and 3 to make room

**Goal:** Reserve vertical space for the taller new Panel #1 (h: 7→9) plus the new Panel #4 (h: 8). After this task panels 2 and 3 will visually slide down by 10 rows with a blank gap above them — that's expected.

**Why this first:** Mechanical, low risk, leaves the dashboard valid and verifiable before any JS gets touched.

**Files:**
- Modify: `grafana/weekly-dashboard.json` — panel 1 `gridPos.h` 7→9, panel 2 `gridPos.y` 7→17, panel 3 `gridPos.y` 25→35

- [ ] **Step 1: Edit panel 1 `gridPos.h` from 7 to 9**

In `grafana/weekly-dashboard.json`, find the panel where `"id": 1`, and inside its `"gridPos"`, change:
```json
"h": 7,
"w": 24,
"x": 0,
"y": 0
```
to:
```json
"h": 9,
"w": 24,
"x": 0,
"y": 0
```

- [ ] **Step 2: Edit panel 2 `gridPos.y` from 7 to 17**

In the same file, find the panel where `"id": 2`, and inside its `"gridPos"`, change:
```json
"h": 18,
"w": 24,
"x": 0,
"y": 7
```
to:
```json
"h": 18,
"w": 24,
"x": 0,
"y": 17
```

- [ ] **Step 3: Edit panel 3 `gridPos.y` from 25 to 35**

Find the panel where `"id": 3`, and inside its `"gridPos"`, change:
```json
"h": 10,
"w": 24,
"x": 0,
"y": 25
```
to:
```json
"h": 10,
"w": 24,
"x": 0,
"y": 35
```

- [ ] **Step 4: Validate JSON**

Run:
```bash
python3 -m json.tool grafana/weekly-dashboard.json > /dev/null && echo OK
```
Expected: `OK`. Anything else (e.g. `json.decoder.JSONDecodeError`) means a typo — fix before continuing.

- [ ] **Step 5: Re-import dashboard**

Run:
```bash
curl -s -u admin:admin -H 'Content-Type: application/json' \
  -X POST --data @grafana/weekly-dashboard.json \
  http://127.0.0.1:3000/api/dashboards/db
```
Expected output contains `"status":"success"` and a new `"version":N` integer.

- [ ] **Step 6: Headless verify**

Run:
```bash
python3 tools/verify_dashboard.py
```
Expected: exit 0, "✅ Dashboard rendered without panel errors" (or equivalent success line). `/tmp/gym-dash.png` should now show panel 1 (old diverging bar) at the top, a blank gap from y=9..y=16, then the heatmap, then the line chart.

- [ ] **Step 7: Commit**

```bash
git add grafana/weekly-dashboard.json
git commit -m "chore(dashboard): reserve gridPos space for new panel 1 + panel 4"
```

---

## Task 2: Replace Panel #1 SQL with Popular Times query

**Goal:** Swap panel 1's `targets[0].rawSql` and `title` for the new Popular Times shape. The panel will look broken (old JS won't understand new columns) until Task 3 finishes — that's expected.

**Files:**
- Modify: `grafana/weekly-dashboard.json` — panel 1 `title` and `targets[0].rawSql`

- [ ] **Step 1: Change panel 1 title**

In `grafana/weekly-dashboard.json`, in the panel where `"id": 1`, change:
```json
"title": "Current vs Same Weekday/Time History",
```
to:
```json
"title": "今日典型人潮 (Popular Times)",
```

- [ ] **Step 2: Replace panel 1 `rawSql`**

In the same panel, replace the entire `rawSql` value (currently the `current_vs_history` ORDER BY query) with this exact string. Because `rawSql` is a JSON string, every newline must be `\n` and every quote escaped — the easiest edit is to replace the whole field on one line:

```json
"rawSql": "-- refresh on: ${palette}\nWITH today AS (\n  SELECT EXTRACT(ISODOW FROM now() AT TIME ZONE 'Asia/Taipei')::int AS wd\n),\nhourly_typical AS (\n  SELECT\n    EXTRACT(HOUR FROM slot_start_time)::int AS hour,\n    ROUND(AVG(avg_current_count))::int      AS typical_count,\n    SUM(sample_count)                       AS samples\n  FROM weekly_occupancy_slots, today\n  WHERE venue = ${venue:sqlstring}\n    AND weekday_number = today.wd\n  GROUP BY hour\n),\ncurrent_now AS (\n  SELECT DISTINCT ON (venue)\n    EXTRACT(HOUR FROM fetched_at AT TIME ZONE 'Asia/Taipei')::int AS hour,\n    current_count\n  FROM occupancy\n  WHERE venue = ${venue:sqlstring}\n  ORDER BY venue, fetched_at DESC\n)\nSELECT 'typical'::text AS kind, hour, typical_count AS count, samples\nFROM hourly_typical\nUNION ALL\nSELECT 'now'::text, hour, current_count, 1\nFROM current_now\nORDER BY kind DESC, hour;"
```

- [ ] **Step 3: Validate JSON**

```bash
python3 -m json.tool grafana/weekly-dashboard.json > /dev/null && echo OK
```
Expected: `OK`.

- [ ] **Step 4: Verify SQL directly against Postgres**

Run the same SQL through `psql` to confirm it returns rows and column names match what the JS in Task 3 expects. The `${venue:sqlstring}` substitution Grafana would do is replaced by a literal here:

```bash
docker exec gym-postgres psql -U songhejun -d gym_fetch -c "WITH today AS (SELECT EXTRACT(ISODOW FROM now() AT TIME ZONE 'Asia/Taipei')::int AS wd), hourly_typical AS (SELECT EXTRACT(HOUR FROM slot_start_time)::int AS hour, ROUND(AVG(avg_current_count))::int AS typical_count, SUM(sample_count) AS samples FROM weekly_occupancy_slots, today WHERE venue = '健身中心' AND weekday_number = today.wd GROUP BY hour), current_now AS (SELECT DISTINCT ON (venue) EXTRACT(HOUR FROM fetched_at AT TIME ZONE 'Asia/Taipei')::int AS hour, current_count FROM occupancy WHERE venue = '健身中心' ORDER BY venue, fetched_at DESC) SELECT 'typical'::text AS kind, hour, typical_count AS count, samples FROM hourly_typical UNION ALL SELECT 'now'::text, hour, current_count, 1 FROM current_now ORDER BY kind DESC, hour;"
```
Expected: rows where `kind` is `typical` (one row per hour 6..21) followed by at most one row where `kind = 'now'`. Column names: `kind, hour, count, samples`. If `kind` rows or column names differ, the JS in Task 3 won't bind correctly — STOP and reconcile before re-importing.

- [ ] **Step 5: Re-import dashboard**

```bash
curl -s -u admin:admin -H 'Content-Type: application/json' \
  -X POST --data @grafana/weekly-dashboard.json \
  http://127.0.0.1:3000/api/dashboards/db
```
Expected: `"status":"success"`.

- [ ] **Step 6: Commit**

```bash
git add grafana/weekly-dashboard.json
git commit -m "feat(panel1): swap SQL to Popular Times query (typical + now)"
```

Note: do NOT run `tools/verify_dashboard.py` yet — the old JS reading `'venue'` / `'difference_from_median'` fields won't find them and will return `{}`, leaving the panel mostly blank. The panel will only stop looking broken after Task 3. The commit boundary here is intentional so a future bisect can pinpoint SQL-vs-JS issues separately.

---

## Task 3: Implement Popular Times JS in Panel #1

**Goal:** Replace panel 1's `options.getOption` JS with the bar-chart-plus-current-hour-highlight implementation. After this task panel 1 fully works.

**Files:**
- Modify: `grafana/weekly-dashboard.json` — panel 1 `options.getOption`

The new JS, as a standalone reference (will be JSON-encoded in the file):

```js
const PALETTES = {
  terracotta: ['#ede4d4','#d8c4a0','#caa479','#b8754f','#974a3a','#642822'],
  sage:       ['#e8e7d6','#c9d2b3','#9eb288','#728c64','#4f6948','#2e4530'],
  slate:      ['#e6ebef','#bdcad4','#8ea1b1','#5e7689','#3d556a','#22344a'],
  rose:       ['#ece1de','#dbb9b9','#c08e95','#9a5e72','#6d3f56','#412236'],
  teal:       ['#dde7e5','#a8c2bf','#6fa097','#487a72','#2e5953','#1a3833'],
  lavender:   ['#e7e1ec','#c5b4d3','#a08abb','#7e639d','#5a417a','#3a2855'],
};
const NEUTRAL_BAR = '#3d556a';
const len = (v) => (typeof v.length === 'number' ? v.length : (v.toArray ? v.toArray().length : 0));
const at  = (v, i) => (typeof v.get === 'function' ? v.get(i) : v[i]);

const series = context.panel.data.series && context.panel.data.series[0];
if (!series || !series.fields) return {};
const f = (n) => { const x = series.fields.find(x => x.name === n); return x ? x.values : null; };
const kindF = f('kind');
const hourF = f('hour');
const cntF  = f('count');
const sampF = f('samples');
if (!kindF || !hourF || !cntF || len(kindF) === 0) return {};

const paletteName = context.grafana.replaceVariables('${palette}');
const stops = PALETTES[paletteName] || PALETTES.slate;
const venueName = context.grafana.replaceVariables('${venue}');

const fmtHour = new Intl.DateTimeFormat('en-GB', { hour: 'numeric', hour12: false, timeZone: 'Asia/Taipei' });
const currentHour = parseInt(fmtHour.format(new Date()), 10);
const weekdayShort = new Intl.DateTimeFormat('en', { weekday: 'short', timeZone: 'Asia/Taipei' }).format(new Date());

const typicalRows = [];
let nowRow = null;
for (let i = 0; i < len(kindF); i++) {
  const kind = at(kindF, i);
  const hour = at(hourF, i);
  const count = at(cntF, i);
  const samples = at(sampF, i);
  if (kind === 'typical') typicalRows.push({ hour, count, samples });
  else if (kind === 'now') nowRow = { hour, count };
}
typicalRows.sort((a, b) => a.hour - b.hour);
if (typicalRows.length === 0) return {};

const hours = typicalRows.map(r => String(r.hour).padStart(2, '0') + ':00');
const barData = typicalRows.map(r => ({
  value: r.count,
  itemStyle: { color: r.hour === currentHour ? stops[5] : NEUTRAL_BAR },
}));

const scatterData = [];
if (nowRow != null) {
  const xIdx = typicalRows.findIndex(r => r.hour === nowRow.hour);
  if (xIdx >= 0) {
    scatterData.push({
      value: [xIdx, nowRow.count],
      label: { show: true, formatter: '現在: ' + nowRow.count + ' 人', position: 'top', color: '#fafafa', fontSize: 12, fontWeight: 'bold' },
      symbol: 'none',
    });
  }
}

return {
  title: {
    text: '今日 (' + weekdayShort + ') · ' + venueName,
    left: 'center', top: 6,
    textStyle: { color: '#ddd', fontSize: 13, fontWeight: 'normal' },
  },
  grid: { left: 50, right: 30, top: 50, bottom: 30, containLabel: true },
  xAxis: { type: 'category', data: hours, axisLabel: { color: '#aaa', fontSize: 10 } },
  yAxis: {
    type: 'value',
    name: '人',
    nameTextStyle: { color: '#888', fontSize: 10 },
    axisLabel: { color: '#888', fontSize: 10 },
    splitLine: { lineStyle: { color: '#222' } },
  },
  tooltip: {
    trigger: 'item',
    formatter: (p) => {
      if (p.seriesType === 'scatter') return '現在: ' + (nowRow ? nowRow.count : '-') + ' 人';
      const r = typicalRows[p.dataIndex];
      return weekdayShort + ' ' + hours[p.dataIndex] + '<br/>' +
             '平均 ' + Number(r.count).toFixed(0) + ' 人<br/>' +
             '樣本 ' + r.samples + ' 週';
    },
  },
  series: [
    { type: 'bar', data: barData, barWidth: '60%' },
    { type: 'scatter', data: scatterData, symbolSize: 0, z: 5 },
  ],
};
```

- [ ] **Step 1: Replace panel 1 `options.getOption`**

In `grafana/weekly-dashboard.json`, find the panel where `"id": 1`, locate its `options.getOption` value (currently the diverging-bar JS), and replace the whole string with this JSON-encoded form. Newlines become `\n`, single quotes stay, no double-quote escapes are needed in this JS (only single quotes inside):

```json
"getOption": "const PALETTES = {\n  terracotta: ['#ede4d4','#d8c4a0','#caa479','#b8754f','#974a3a','#642822'],\n  sage:       ['#e8e7d6','#c9d2b3','#9eb288','#728c64','#4f6948','#2e4530'],\n  slate:      ['#e6ebef','#bdcad4','#8ea1b1','#5e7689','#3d556a','#22344a'],\n  rose:       ['#ece1de','#dbb9b9','#c08e95','#9a5e72','#6d3f56','#412236'],\n  teal:       ['#dde7e5','#a8c2bf','#6fa097','#487a72','#2e5953','#1a3833'],\n  lavender:   ['#e7e1ec','#c5b4d3','#a08abb','#7e639d','#5a417a','#3a2855'],\n};\nconst NEUTRAL_BAR = '#3d556a';\nconst len = (v) => (typeof v.length === 'number' ? v.length : (v.toArray ? v.toArray().length : 0));\nconst at  = (v, i) => (typeof v.get === 'function' ? v.get(i) : v[i]);\n\nconst series = context.panel.data.series && context.panel.data.series[0];\nif (!series || !series.fields) return {};\nconst f = (n) => { const x = series.fields.find(x => x.name === n); return x ? x.values : null; };\nconst kindF = f('kind');\nconst hourF = f('hour');\nconst cntF  = f('count');\nconst sampF = f('samples');\nif (!kindF || !hourF || !cntF || len(kindF) === 0) return {};\n\nconst paletteName = context.grafana.replaceVariables('${palette}');\nconst stops = PALETTES[paletteName] || PALETTES.slate;\nconst venueName = context.grafana.replaceVariables('${venue}');\n\nconst fmtHour = new Intl.DateTimeFormat('en-GB', { hour: 'numeric', hour12: false, timeZone: 'Asia/Taipei' });\nconst currentHour = parseInt(fmtHour.format(new Date()), 10);\nconst weekdayShort = new Intl.DateTimeFormat('en', { weekday: 'short', timeZone: 'Asia/Taipei' }).format(new Date());\n\nconst typicalRows = [];\nlet nowRow = null;\nfor (let i = 0; i < len(kindF); i++) {\n  const kind = at(kindF, i);\n  const hour = at(hourF, i);\n  const count = at(cntF, i);\n  const samples = at(sampF, i);\n  if (kind === 'typical') typicalRows.push({ hour, count, samples });\n  else if (kind === 'now') nowRow = { hour, count };\n}\ntypicalRows.sort((a, b) => a.hour - b.hour);\nif (typicalRows.length === 0) return {};\n\nconst hours = typicalRows.map(r => String(r.hour).padStart(2, '0') + ':00');\nconst barData = typicalRows.map(r => ({\n  value: r.count,\n  itemStyle: { color: r.hour === currentHour ? stops[5] : NEUTRAL_BAR },\n}));\n\nconst scatterData = [];\nif (nowRow != null) {\n  const xIdx = typicalRows.findIndex(r => r.hour === nowRow.hour);\n  if (xIdx >= 0) {\n    scatterData.push({\n      value: [xIdx, nowRow.count],\n      label: { show: true, formatter: '現在: ' + nowRow.count + ' 人', position: 'top', color: '#fafafa', fontSize: 12, fontWeight: 'bold' },\n      symbol: 'none',\n    });\n  }\n}\n\nreturn {\n  title: { text: '今日 (' + weekdayShort + ') · ' + venueName, left: 'center', top: 6, textStyle: { color: '#ddd', fontSize: 13, fontWeight: 'normal' } },\n  grid: { left: 50, right: 30, top: 50, bottom: 30, containLabel: true },\n  xAxis: { type: 'category', data: hours, axisLabel: { color: '#aaa', fontSize: 10 } },\n  yAxis: { type: 'value', name: '人', nameTextStyle: { color: '#888', fontSize: 10 }, axisLabel: { color: '#888', fontSize: 10 }, splitLine: { lineStyle: { color: '#222' } } },\n  tooltip: {\n    trigger: 'item',\n    formatter: (p) => {\n      if (p.seriesType === 'scatter') return '現在: ' + (nowRow ? nowRow.count : '-') + ' 人';\n      const r = typicalRows[p.dataIndex];\n      return weekdayShort + ' ' + hours[p.dataIndex] + '<br/>' + '平均 ' + Number(r.count).toFixed(0) + ' 人<br/>' + '樣本 ' + r.samples + ' 週';\n    },\n  },\n  series: [\n    { type: 'bar', data: barData, barWidth: '60%' },\n    { type: 'scatter', data: scatterData, symbolSize: 0, z: 5 },\n  ],\n};\n"
```

- [ ] **Step 2: Validate JSON**

```bash
python3 -m json.tool grafana/weekly-dashboard.json > /dev/null && echo OK
```
Expected: `OK`.

- [ ] **Step 3: Re-import dashboard**

```bash
curl -s -u admin:admin -H 'Content-Type: application/json' \
  -X POST --data @grafana/weekly-dashboard.json \
  http://127.0.0.1:3000/api/dashboards/db
```
Expected: `"status":"success"`.

- [ ] **Step 4: Headless verify panel #1 renders cleanly**

```bash
python3 tools/verify_dashboard.py
```
Expected: exit 0. Read `/tmp/gym-dash.png` — panel 1 should show a bar chart with 16 bars (06:00..21:00), one bar darkened (the current hour), with "現在: N 人" text floating above the current hour's bar. Title reads "今日 (Tue) · 健身中心" (weekday/venue per current state).

If you see "ReferenceError" or "is not defined" in `verify_dashboard.py` output, the most likely cause is a bare `data` or `replaceVariables(` left in the JS string — grep for `\\\"data\\.` or `replaceVariables\\(` inside the JSON to find it.

- [ ] **Step 5: Commit**

```bash
git add grafana/weekly-dashboard.json
git commit -m "feat(panel1): implement Popular Times bar chart with current-hour highlight"
```

---

## Task 4: Add Panel #4 entry (Live Cards) — SQL + skeleton JS

**Goal:** Append a new panel object (`"id": 4`) at the end of the `panels` array. Use a minimal `getOption` returning `{}` so we can confirm the panel container appears and the SQL runs without error before tackling the multi-grid layout.

**Files:**
- Modify: `grafana/weekly-dashboard.json` — append new panel after panel id 3

- [ ] **Step 1: Insert the new panel object before the closing `]` of `panels`**

Inside `grafana/weekly-dashboard.json`, locate the closing `]` of the `"panels"` array (last `}` of panel id 3 followed by `]`). Insert a comma after panel 3's closing brace, then this object:

```json
{
  "id": 4,
  "type": "volkovlabs-echarts-panel",
  "title": "venue 現況 (Live Cards)",
  "gridPos": { "h": 8, "w": 24, "x": 0, "y": 9 },
  "datasource": { "type": "grafana-postgresql-datasource", "uid": "bfm1ctqr9jgn4b" },
  "targets": [
    {
      "refId": "A",
      "datasource": { "type": "grafana-postgresql-datasource", "uid": "bfm1ctqr9jgn4b" },
      "format": "table",
      "rawQuery": true,
      "editorMode": "code",
      "rawSql": "-- refresh on: ${palette}\nWITH today_start AS (\n  SELECT (date_trunc('day', now() AT TIME ZONE 'Asia/Taipei') AT TIME ZONE 'Asia/Taipei') AS ts\n),\nlatest_meta AS (\n  SELECT DISTINCT ON (venue)\n    venue, comfortable_count, capacity_count\n  FROM occupancy\n  WHERE comfortable_count IS NOT NULL OR capacity_count IS NOT NULL\n  ORDER BY venue, fetched_at DESC\n),\nreadings_today AS (\n  SELECT venue, fetched_at, current_count\n  FROM occupancy, today_start\n  WHERE fetched_at >= today_start.ts\n)\nSELECT\n  'summary'::text AS kind,\n  cvh.venue,\n  EXTRACT(EPOCH FROM cvh.latest_fetched_at) * 1000 AS ts_ms,\n  cvh.current_count,\n  cvh.historical_median_current_count   AS hist_median,\n  cvh.difference_from_median            AS diff,\n  cvh.current_crowding_ratio            AS ratio,\n  meta.comfortable_count                AS comfortable,\n  meta.capacity_count                   AS capacity\nFROM current_vs_history cvh\nLEFT JOIN latest_meta meta ON meta.venue = cvh.venue\nUNION ALL\nSELECT\n  'reading'::text, venue,\n  EXTRACT(EPOCH FROM fetched_at) * 1000,\n  current_count,\n  NULL, NULL, NULL, NULL, NULL\nFROM readings_today\nORDER BY kind DESC, venue, ts_ms;"
    }
  ],
  "fieldConfig": { "defaults": {}, "overrides": [] },
  "options": {
    "getOption": "return {};",
    "renderer": "canvas",
    "themeEditor": { "config": {}, "name": "default" },
    "visualEditor": { "code": "", "codeEditor": "svg", "dataset": [] }
  }
}
```

- [ ] **Step 2: Validate JSON**

```bash
python3 -m json.tool grafana/weekly-dashboard.json > /dev/null && echo OK
```
Expected: `OK`. If you see a JSON error here, double-check the comma you inserted after panel id 3.

- [ ] **Step 3: Verify SQL directly against Postgres**

```bash
docker exec gym-postgres psql -U songhejun -d gym_fetch -c "WITH today_start AS (SELECT (date_trunc('day', now() AT TIME ZONE 'Asia/Taipei') AT TIME ZONE 'Asia/Taipei') AS ts), latest_meta AS (SELECT DISTINCT ON (venue) venue, comfortable_count, capacity_count FROM occupancy WHERE comfortable_count IS NOT NULL OR capacity_count IS NOT NULL ORDER BY venue, fetched_at DESC), readings_today AS (SELECT venue, fetched_at, current_count FROM occupancy, today_start WHERE fetched_at >= today_start.ts) SELECT 'summary'::text AS kind, cvh.venue, EXTRACT(EPOCH FROM cvh.latest_fetched_at) * 1000 AS ts_ms, cvh.current_count, cvh.historical_median_current_count AS hist_median, cvh.difference_from_median AS diff, cvh.current_crowding_ratio AS ratio, meta.comfortable_count AS comfortable, meta.capacity_count AS capacity FROM current_vs_history cvh LEFT JOIN latest_meta meta ON meta.venue = cvh.venue UNION ALL SELECT 'reading'::text, venue, EXTRACT(EPOCH FROM fetched_at) * 1000, current_count, NULL, NULL, NULL, NULL, NULL FROM readings_today ORDER BY kind DESC, venue, ts_ms;" | head -30
```
Expected: two `summary` rows (one per venue) followed by many `reading` rows for today. Columns: `kind, venue, ts_ms, current_count, hist_median, diff, ratio, comfortable, capacity`. If the panel hasn't run yet today, `readings_today` may be empty — that's still a valid result (two `summary` rows only), and Task 5's JS handles it.

- [ ] **Step 4: Re-import dashboard**

```bash
curl -s -u admin:admin -H 'Content-Type: application/json' \
  -X POST --data @grafana/weekly-dashboard.json \
  http://127.0.0.1:3000/api/dashboards/db
```
Expected: `"status":"success"`.

- [ ] **Step 5: Headless verify the new empty panel exists with no errors**

```bash
python3 tools/verify_dashboard.py
```
Expected: exit 0. `/tmp/gym-dash.png` should show panel 1 (Popular Times) at top, then a blank panel labelled "venue 現況 (Live Cards)" (because `getOption` returned `{}`), then the heatmap, then the line chart. No console errors.

- [ ] **Step 6: Commit**

```bash
git add grafana/weekly-dashboard.json
git commit -m "feat(panel4): add Live Cards panel skeleton with SQL only"
```

---

## Task 5: Implement Live Cards JS (Panel #4)

**Goal:** Replace panel 4's stub `getOption` with the two-grid sparkline + text-overlay implementation.

**Files:**
- Modify: `grafana/weekly-dashboard.json` — panel 4 `options.getOption`

**Implementation notes:**

- The spec described "absolute pixel offsets within the panel" for the text overlays, but ECharts `graphic.elements` accepts percent strings (`'25%'`) which keeps the layout robust as the panel resizes; this plan uses percents. Vertical positions are pixel offsets from the panel top because the panel height is fixed (gridPos h=8).
- The big-number colour uses the heatmap's `interpStops(stops, ratioPct)` helper so it picks a stop along the chosen palette based on `crowding_ratio` (0..100). The palette stops are duplicated here — there is no shared scope across Grafana panels.
- Sparkline gap-padding mirrors panel #3: insert `[t-1, null]` and `[t+1, null]` between adjacent points more than 60 minutes apart so the line breaks visually rather than bridging closed-hour gaps. (Within today only, the dashboard's default time range; long gaps mostly occur if the dashboard is opened across a midnight boundary.)
- Diff arrow + colour: `↑` warm `palette[3]` if diff > 0; `↓` cool `palette[1]` if diff < 0; `–` neutral `#888` if diff == 0.

Standalone JS reference:

```js
const PALETTES = {
  terracotta: ['#ede4d4','#d8c4a0','#caa479','#b8754f','#974a3a','#642822'],
  sage:       ['#e8e7d6','#c9d2b3','#9eb288','#728c64','#4f6948','#2e4530'],
  slate:      ['#e6ebef','#bdcad4','#8ea1b1','#5e7689','#3d556a','#22344a'],
  rose:       ['#ece1de','#dbb9b9','#c08e95','#9a5e72','#6d3f56','#412236'],
  teal:       ['#dde7e5','#a8c2bf','#6fa097','#487a72','#2e5953','#1a3833'],
  lavender:   ['#e7e1ec','#c5b4d3','#a08abb','#7e639d','#5a417a','#3a2855'],
};
const len = (v) => (typeof v.length === 'number' ? v.length : (v.toArray ? v.toArray().length : 0));
const at  = (v, i) => (typeof v.get === 'function' ? v.get(i) : v[i]);
const hexToRgb = (h) => [parseInt(h.slice(1,3),16), parseInt(h.slice(3,5),16), parseInt(h.slice(5,7),16)];
const lerpChan = (a, b, t) => Math.round(a + (b - a) * t);
const interpStops = (stops, r) => {
  if (r <= 0) return stops[0];
  if (r >= 100) return stops[5];
  const idx = r / 20;
  const i0 = Math.floor(idx);
  const i1 = Math.min(5, i0 + 1);
  const t = idx - i0;
  const c0 = hexToRgb(stops[i0]); const c1 = hexToRgb(stops[i1]);
  return '#' + [0,1,2].map(k => lerpChan(c0[k], c1[k], t).toString(16).padStart(2, '0')).join('');
};

const series = context.panel.data.series && context.panel.data.series[0];
if (!series || !series.fields) return {};
const f = (n) => { const x = series.fields.find(x => x.name === n); return x ? x.values : null; };
const kindF = f('kind');
const venueF = f('venue');
const tsF = f('ts_ms');
const cntF = f('current_count');
const medF = f('hist_median');
const difF = f('diff');
const ratioF = f('ratio');
const cmfF = f('comfortable');
const capF = f('capacity');
if (!kindF || !venueF || !cntF || len(kindF) === 0) return {};

const paletteName = context.grafana.replaceVariables('${palette}');
const stops = PALETTES[paletteName] || PALETTES.slate;

const venues = {};
for (let i = 0; i < len(kindF); i++) {
  const kind = at(kindF, i);
  const venue = at(venueF, i);
  if (!venues[venue]) venues[venue] = { summary: null, readings: [] };
  if (kind === 'summary') {
    venues[venue].summary = {
      count: at(cntF, i),
      median: at(medF, i),
      diff: at(difF, i),
      ratio: at(ratioF, i),
      comfortable: at(cmfF, i),
      capacity: at(capF, i),
    };
  } else if (kind === 'reading') {
    venues[venue].readings.push([at(tsF, i), at(cntF, i)]);
  }
}
const venueNames = Object.keys(venues).sort();
if (venueNames.length === 0) return {};

const padReadings = (rows) => {
  if (rows.length <= 1) return rows;
  const out = [rows[0]];
  for (let i = 1; i < rows.length; i++) {
    const gapMs = rows[i][0] - rows[i-1][0];
    if (gapMs > 60 * 60 * 1000) {
      out.push([rows[i-1][0] + 1, null]);
      out.push([rows[i][0] - 1, null]);
    }
    out.push(rows[i]);
  }
  return out;
};

const grids = [];
const xAxes = [];
const yAxes = [];
const seriesOut = [];
const graphicEls = [];
const lineColor = stops[3];

venueNames.slice(0, 2).forEach((vname, idx) => {
  const isLeft = idx === 0;
  const leftPct = isLeft ? 2 : 52;
  const rightPct = isLeft ? 52 : 2;
  grids.push({ left: leftPct + '%', right: rightPct + '%', top: 110, bottom: 14 });
  xAxes.push({ type: 'time', show: false, gridIndex: idx });
  yAxes.push({ type: 'value', show: false, gridIndex: idx, min: 0 });

  const v = venues[vname];
  const data = padReadings(v.readings || []);
  const lastIdx = data.length - 1;
  seriesOut.push({
    type: 'line',
    name: vname,
    data: data,
    xAxisIndex: idx,
    yAxisIndex: idx,
    showSymbol: false,
    connectNulls: false,
    lineStyle: { color: lineColor, width: 1.6 },
    itemStyle: { color: lineColor },
    markPoint: lastIdx >= 0 && data[lastIdx][1] != null ? {
      symbol: 'circle', symbolSize: 8,
      itemStyle: { color: '#fff', borderColor: lineColor, borderWidth: 1.5 },
      label: { show: false },
      data: [{ coord: data[lastIdx] }],
    } : undefined,
  });

  const s = v.summary;
  if (!s) return;
  const ratioPct = Math.round((s.ratio || 0) * 100);
  const bigCol = interpStops(stops, ratioPct);
  const cmfPct = s.comfortable ? Math.round((s.count / s.comfortable) * 100) + '%' : '—';
  const capPct = s.capacity ? Math.round((s.count / s.capacity) * 100) + '%' : '—';
  const subtitle = '舒適 ' + cmfPct + ' · 容量 ' + capPct;
  const diff = Number(s.diff || 0);
  const diffColor = diff > 0 ? stops[3] : (diff < 0 ? stops[1] : '#888');
  const arrow = diff > 0 ? '↑' : (diff < 0 ? '↓' : '–');
  const diffMag = Math.abs(diff).toFixed(0);
  const diffText = arrow + ' ' + diffMag + ' vs 同時段中位數';
  const xCenterPct = isLeft ? '25%' : '75%';

  graphicEls.push(
    { type: 'text', left: xCenterPct, top: 12, style: { text: vname, fill: '#9aa', fontSize: 12, textAlign: 'center' } },
    { type: 'text', left: xCenterPct, top: 30, style: { text: String(s.count), fill: bigCol, fontSize: 40, fontWeight: 'lighter', textAlign: 'center' } },
    { type: 'text', left: xCenterPct, top: 78, style: { text: subtitle, fill: '#888', fontSize: 11, textAlign: 'center' } },
    { type: 'text', left: xCenterPct, top: 94, style: { text: diffText, fill: diffColor, fontSize: 12, textAlign: 'center' } },
  );
});

return {
  grid: grids,
  xAxis: xAxes,
  yAxis: yAxes,
  series: seriesOut,
  graphic: { elements: graphicEls },
  tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
};
```

- [ ] **Step 1: Replace panel 4 `options.getOption` (currently `"return {};"`)**

Locate the panel with `"id": 4` and replace its `"getOption"` value with this JSON-encoded string:

```json
"getOption": "const PALETTES = {\n  terracotta: ['#ede4d4','#d8c4a0','#caa479','#b8754f','#974a3a','#642822'],\n  sage:       ['#e8e7d6','#c9d2b3','#9eb288','#728c64','#4f6948','#2e4530'],\n  slate:      ['#e6ebef','#bdcad4','#8ea1b1','#5e7689','#3d556a','#22344a'],\n  rose:       ['#ece1de','#dbb9b9','#c08e95','#9a5e72','#6d3f56','#412236'],\n  teal:       ['#dde7e5','#a8c2bf','#6fa097','#487a72','#2e5953','#1a3833'],\n  lavender:   ['#e7e1ec','#c5b4d3','#a08abb','#7e639d','#5a417a','#3a2855'],\n};\nconst len = (v) => (typeof v.length === 'number' ? v.length : (v.toArray ? v.toArray().length : 0));\nconst at  = (v, i) => (typeof v.get === 'function' ? v.get(i) : v[i]);\nconst hexToRgb = (h) => [parseInt(h.slice(1,3),16), parseInt(h.slice(3,5),16), parseInt(h.slice(5,7),16)];\nconst lerpChan = (a, b, t) => Math.round(a + (b - a) * t);\nconst interpStops = (stops, r) => {\n  if (r <= 0) return stops[0];\n  if (r >= 100) return stops[5];\n  const idx = r / 20;\n  const i0 = Math.floor(idx);\n  const i1 = Math.min(5, i0 + 1);\n  const t = idx - i0;\n  const c0 = hexToRgb(stops[i0]); const c1 = hexToRgb(stops[i1]);\n  return '#' + [0,1,2].map(k => lerpChan(c0[k], c1[k], t).toString(16).padStart(2, '0')).join('');\n};\n\nconst series = context.panel.data.series && context.panel.data.series[0];\nif (!series || !series.fields) return {};\nconst f = (n) => { const x = series.fields.find(x => x.name === n); return x ? x.values : null; };\nconst kindF = f('kind');\nconst venueF = f('venue');\nconst tsF = f('ts_ms');\nconst cntF = f('current_count');\nconst medF = f('hist_median');\nconst difF = f('diff');\nconst ratioF = f('ratio');\nconst cmfF = f('comfortable');\nconst capF = f('capacity');\nif (!kindF || !venueF || !cntF || len(kindF) === 0) return {};\n\nconst paletteName = context.grafana.replaceVariables('${palette}');\nconst stops = PALETTES[paletteName] || PALETTES.slate;\n\nconst venues = {};\nfor (let i = 0; i < len(kindF); i++) {\n  const kind = at(kindF, i);\n  const venue = at(venueF, i);\n  if (!venues[venue]) venues[venue] = { summary: null, readings: [] };\n  if (kind === 'summary') {\n    venues[venue].summary = { count: at(cntF, i), median: at(medF, i), diff: at(difF, i), ratio: at(ratioF, i), comfortable: at(cmfF, i), capacity: at(capF, i) };\n  } else if (kind === 'reading') {\n    venues[venue].readings.push([at(tsF, i), at(cntF, i)]);\n  }\n}\nconst venueNames = Object.keys(venues).sort();\nif (venueNames.length === 0) return {};\n\nconst padReadings = (rows) => {\n  if (rows.length <= 1) return rows;\n  const out = [rows[0]];\n  for (let i = 1; i < rows.length; i++) {\n    const gapMs = rows[i][0] - rows[i-1][0];\n    if (gapMs > 60 * 60 * 1000) {\n      out.push([rows[i-1][0] + 1, null]);\n      out.push([rows[i][0] - 1, null]);\n    }\n    out.push(rows[i]);\n  }\n  return out;\n};\n\nconst grids = [];\nconst xAxes = [];\nconst yAxes = [];\nconst seriesOut = [];\nconst graphicEls = [];\nconst lineColor = stops[3];\n\nvenueNames.slice(0, 2).forEach((vname, idx) => {\n  const isLeft = idx === 0;\n  const leftPct = isLeft ? 2 : 52;\n  const rightPct = isLeft ? 52 : 2;\n  grids.push({ left: leftPct + '%', right: rightPct + '%', top: 110, bottom: 14 });\n  xAxes.push({ type: 'time', show: false, gridIndex: idx });\n  yAxes.push({ type: 'value', show: false, gridIndex: idx, min: 0 });\n\n  const v = venues[vname];\n  const data = padReadings(v.readings || []);\n  const lastIdx = data.length - 1;\n  seriesOut.push({\n    type: 'line', name: vname, data: data,\n    xAxisIndex: idx, yAxisIndex: idx,\n    showSymbol: false, connectNulls: false,\n    lineStyle: { color: lineColor, width: 1.6 },\n    itemStyle: { color: lineColor },\n    markPoint: lastIdx >= 0 && data[lastIdx][1] != null ? {\n      symbol: 'circle', symbolSize: 8,\n      itemStyle: { color: '#fff', borderColor: lineColor, borderWidth: 1.5 },\n      label: { show: false },\n      data: [{ coord: data[lastIdx] }],\n    } : undefined,\n  });\n\n  const s = v.summary;\n  if (!s) return;\n  const ratioPct = Math.round((s.ratio || 0) * 100);\n  const bigCol = interpStops(stops, ratioPct);\n  const cmfPct = s.comfortable ? Math.round((s.count / s.comfortable) * 100) + '%' : '—';\n  const capPct = s.capacity ? Math.round((s.count / s.capacity) * 100) + '%' : '—';\n  const subtitle = '舒適 ' + cmfPct + ' · 容量 ' + capPct;\n  const diff = Number(s.diff || 0);\n  const diffColor = diff > 0 ? stops[3] : (diff < 0 ? stops[1] : '#888');\n  const arrow = diff > 0 ? '↑' : (diff < 0 ? '↓' : '–');\n  const diffText = arrow + ' ' + Math.abs(diff).toFixed(0) + ' vs 同時段中位數';\n  const xCenterPct = isLeft ? '25%' : '75%';\n  graphicEls.push(\n    { type: 'text', left: xCenterPct, top: 12, style: { text: vname, fill: '#9aa', fontSize: 12, textAlign: 'center' } },\n    { type: 'text', left: xCenterPct, top: 30, style: { text: String(s.count), fill: bigCol, fontSize: 40, fontWeight: 'lighter', textAlign: 'center' } },\n    { type: 'text', left: xCenterPct, top: 78, style: { text: subtitle, fill: '#888', fontSize: 11, textAlign: 'center' } },\n    { type: 'text', left: xCenterPct, top: 94, style: { text: diffText, fill: diffColor, fontSize: 12, textAlign: 'center' } },\n  );\n});\n\nreturn {\n  grid: grids,\n  xAxis: xAxes,\n  yAxis: yAxes,\n  series: seriesOut,\n  graphic: { elements: graphicEls },\n  tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },\n};\n"
```

- [ ] **Step 2: Validate JSON**

```bash
python3 -m json.tool grafana/weekly-dashboard.json > /dev/null && echo OK
```
Expected: `OK`.

- [ ] **Step 3: Re-import dashboard**

```bash
curl -s -u admin:admin -H 'Content-Type: application/json' \
  -X POST --data @grafana/weekly-dashboard.json \
  http://127.0.0.1:3000/api/dashboards/db
```
Expected: `"status":"success"`.

- [ ] **Step 4: Headless verify**

```bash
python3 tools/verify_dashboard.py
```
Expected: exit 0. `/tmp/gym-dash.png` should now show:
1. Panel 1: Popular Times bars with current-hour highlight at top.
2. Panel 4: two side-by-side cards. Each card shows the venue name at top, a big tinted number (the current count), then "舒適 X% · 容量 Y%", then a coloured arrow + "↑/↓ N vs 同時段中位數" line, with a low sparkline below.
3. Panel 2: heatmap (unchanged).
4. Panel 3: line chart (unchanged).

- [ ] **Step 5: Commit**

```bash
git add grafana/weekly-dashboard.json
git commit -m "feat(panel4): implement Live Cards with sparkline + delta vs median"
```

---

## Task 6: Update CLAUDE.md with the new 4-panel layout

**Goal:** Add one bullet under "Architecture notes" so future agents see the new structure without re-reading the JSON.

**Files:**
- Modify: `CLAUDE.md` — append a bullet under the existing architecture-notes section

- [ ] **Step 1: Read the current CLAUDE.md to locate the "Grafana dashboard" architecture note**

Run:
```bash
grep -n "Grafana dashboard is wired" CLAUDE.md
```
Expected: a single line number — the start of the existing architecture note about the hardcoded datasource UID. The new bullet goes immediately after that paragraph ends.

- [ ] **Step 2: Insert the new bullet**

In `CLAUDE.md`, immediately after the paragraph that begins "**Grafana dashboard is wired to a hardcoded datasource UID.**" (the paragraph ends with the sentence about `${granularity}` and `${palette}` template variables), add a blank line and this new paragraph:

```markdown
**The dashboard has four panels stacked top-to-bottom:** (1) `今日典型人潮` Popular Times bar chart driven by `weekly_occupancy_slots` filtered to today's `ISODOW`, with the current hour highlighted via `palette[5]`; (4) `venue 現況` two-up Live Cards with sparkline of today's `occupancy` readings and a delta-vs-historical-median row driven by `current_vs_history`; (2) `Weekly 30-Minute Occupancy Heatmap` (unchanged); (3) `Selected Venue Exact Counts` line chart (unchanged). Panel 1 and 4 both read `${venue}` and `${palette}` only — no new template variables.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: describe new four-panel layout in CLAUDE.md"
```

---

## Task 7: Visual matrix verification

**Goal:** Confirm `palette` and `venue` variable changes propagate correctly to both new panels, and that nothing has visually regressed.

This is a manual / browser-driven task — no code changes. The agent should walk through the matrix and record observations in the commit message of the next code change (or no commit if nothing needs fixing).

**No file changes.**

- [ ] **Step 1: Confirm dashboard is current**

```bash
curl -s -u admin:admin http://127.0.0.1:3000/api/dashboards/uid/gym-weekly-display | python3 -c "import sys, json; d = json.load(sys.stdin); print('panels:', [p['id'] for p in d['dashboard']['panels']]); print('panel1 title:', next(p['title'] for p in d['dashboard']['panels'] if p['id']==1)); print('panel4 title:', next(p['title'] for p in d['dashboard']['panels'] if p['id']==4))"
```
Expected:
```
panels: [1, 2, 3, 4]
panel1 title: 今日典型人潮 (Popular Times)
panel4 title: venue 現況 (Live Cards)
```

- [ ] **Step 2: Cycle palettes against panel 1**

Open `http://localhost:3000/d/gym-weekly-display/gym-weekly-occupancy` in a browser. For each palette value (`terracotta`, `sage`, `slate`, `rose`, `teal`, `lavender`) selected from the dropdown:
- Panel 1: the *current-hour* bar should change colour to the palette's stop[5] (darkest). Non-current bars stay the same neutral `#3d556a`.
- Panel 4: the big number's colour and the diff arrow's colour should both shift along the chosen palette.

Record any palette where the change fails to propagate — that means the `-- refresh on: ${palette}` sentinel isn't present in that panel's SQL, or Grafana didn't pick it up; re-check the `rawSql` of the affected panel.

- [ ] **Step 3: Switch venue**

Switch `${venue}` between the two venue options.
- Panel 1: should re-query and show the new venue's hourly profile. Title text updates accordingly.
- Panel 4: should NOT change — both venues are always shown in the two-up layout. Confirm this; if it does change, panel 4's query is incorrectly filtering by `${venue}` and must be fixed (Task 4's SQL has no `${venue}` substitution — it must stay that way).

- [ ] **Step 4: Run final headless verify**

```bash
python3 tools/verify_dashboard.py
```
Expected: exit 0, screenshot at `/tmp/gym-dash.png` shows all four panels rendered without overlay errors. This is the final passing-state artifact.

- [ ] **Step 5: No commit needed if all checks pass**

If anything failed and required a fix, the fix gets its own commit with a clear message describing what regressed and how it was fixed. Otherwise no commit.

---

## Done state

After all tasks:
- `git log --oneline -8` shows commits for each task (gridPos shift, panel 1 SQL, panel 1 JS, panel 4 skeleton, panel 4 JS, CLAUDE.md).
- `tools/verify_dashboard.py` exits 0.
- `/tmp/gym-dash.png` shows the new layout exactly as in the spec's Layout table.
- `git status` clean.
- The user can review `/tmp/gym-dash.png` and decide between B and D over a few days of actual use.
