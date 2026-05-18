# Weekly Heatmap Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the weekly dashboard's table-cell heatmap with a runtime-configurable Apache ECharts heatmap, while keeping the comparison table on the native table panel and aligning the venue-counts timeseries to the same palette.

**Architecture:** One Grafana plugin (`volkovlabs-echarts-panel`) is installed into the running `gym-grafana` container. The dashboard JSON gains two custom template variables (`granularity`, `palette`). Panel #1 stays type `table` with a new static diverging palette on the `difference_from_median` column. Panels #2 and #3 switch to `volkovlabs-echarts-panel`, reading the two new variables (plus the existing `metric` and `venue`) at render time. Per-query SQL bucketing handles the 30-min/60-min toggle; no view changes.

**Tech Stack:** Grafana 13 (`gym-grafana` container) · PostgreSQL 18 (`gym-postgres` container) · `volkovlabs-echarts-panel` plugin (Apache ECharts wrapper) · Python `json` stdlib for programmatic JSON edits.

**Spec reference:** `docs/superpowers/specs/2026-05-18-weekly-heatmap-redesign-design.md`.

---

## Conventions used across tasks

**Re-importing the dashboard JSON** after any edit:

```bash
curl -s -u admin:admin -H 'Content-Type: application/json' \
  -X POST --data @grafana/weekly-dashboard.json \
  http://127.0.0.1:3000/api/dashboards/db
```

Expected response includes `"status":"success"` and the new `version` number. If you see `"message":"..."` with no `status:success`, the JSON is malformed — fix before continuing.

**Reading the live dashboard state** to verify edits landed:

```bash
curl -s -u admin:admin http://127.0.0.1:3000/api/dashboards/uid/gym-weekly-display
```

**Dashboard URL for visual verification:** http://localhost:3000/d/gym-weekly-display/gym-weekly-occupancy

**JSON editing pattern.** When tasks below say "edit the JSON", the cleanest way is a small Python one-liner that loads, mutates, and re-dumps with `ensure_ascii=False, indent=2`. The plan provides the exact script per task.

---

## Task 1: Install the volkovlabs-echarts-panel plugin in gym-grafana

**Files:**
- No file changes — operational setup of the running container.

- [ ] **Step 1: Verify the plugin is NOT yet installed**

Run:
```bash
curl -s -u admin:admin http://127.0.0.1:3000/api/plugins | \
  python3 -c "import json,sys; ids=[p['id'] for p in json.load(sys.stdin)]; print('volkovlabs-echarts-panel' in ids)"
```
Expected output: `False`

If output is `True`, skip to Step 4.

- [ ] **Step 2: Install the plugin inside the container**

Run:
```bash
docker exec gym-grafana grafana cli plugins install volkovlabs-echarts-panel
```
Expected: a line like `✔ Installed volkovlabs-echarts-panel successfully`.

- [ ] **Step 3: Restart Grafana so the plugin loads**

Run:
```bash
docker restart gym-grafana
until curl -sS http://127.0.0.1:3000/api/health >/dev/null; do sleep 1; done
echo "grafana back up"
```
Expected: prints `grafana back up` within ~10 seconds.

- [ ] **Step 4: Verify the plugin is now installed and signed**

Run:
```bash
curl -s -u admin:admin http://127.0.0.1:3000/api/plugins | \
  python3 -c "import json,sys; rows=[p for p in json.load(sys.stdin) if p['id']=='volkovlabs-echarts-panel']; print(rows[0]['id'], rows[0].get('signature','no-sig'), rows[0].get('info',{}).get('version','?'))"
```
Expected output: `volkovlabs-echarts-panel valid <version>` (signature `valid`, any version is fine).

- [ ] **Step 5: No commit** — plugin install is environmental setup, not tracked in git. Task 6 (README update) documents this step for future re-builds.

---

## Task 2: Add `granularity` and `palette` template variables

**Files:**
- Modify: `grafana/weekly-dashboard.json` — `dashboard.templating.list` (append two entries)

- [ ] **Step 1: Edit the JSON to append the two new variables**

Run:
```bash
python3 <<'PY'
import json
path = 'grafana/weekly-dashboard.json'
with open(path) as f:
    d = json.load(f)
tlist = d['dashboard']['templating']['list']
# Append granularity if not already present
if not any(v['name'] == 'granularity' for v in tlist):
    tlist.append({
        "name": "granularity",
        "type": "custom",
        "label": "Granularity",
        "query": "30min,60min",
        "current": {"selected": True, "text": "60min", "value": "60min"},
    })
# Append palette if not already present
if not any(v['name'] == 'palette' for v in tlist):
    tlist.append({
        "name": "palette",
        "type": "custom",
        "label": "Palette",
        "query": "terracotta,sage,slate,rose,teal,lavender",
        "current": {"selected": True, "text": "slate", "value": "slate"},
    })
with open(path, 'w') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write('\n')
print("granularity and palette appended")
PY
```
Expected output: `granularity and palette appended`.

- [ ] **Step 2: Re-import the dashboard**

Run the re-import command from the Conventions section. Expected: response includes `"status":"success"`.

- [ ] **Step 3: Verify both variables are live in Grafana**

Run:
```bash
curl -s -u admin:admin http://127.0.0.1:3000/api/dashboards/uid/gym-weekly-display | \
  python3 -c "import json,sys; vs=json.load(sys.stdin)['dashboard']['templating']['list']; print([(v['name'],v.get('current',{}).get('value')) for v in vs])"
```
Expected output:
```
[('venue', '健身中心'), ('metric', 'avg_count'), ('granularity', '60min'), ('palette', 'slate')]
```

- [ ] **Step 4: Commit**

Run:
```bash
git add grafana/weekly-dashboard.json
git commit -m "feat(dashboard): add granularity and palette template variables"
```

---

## Task 3: Replace Panel #1 `difference_from_median` thresholds with the static Morandi diverging palette

**Files:**
- Modify: `grafana/weekly-dashboard.json` — the `byName: difference_from_median` override in panel id 1

- [ ] **Step 1: Edit the threshold steps and cellOptions mode**

Run:
```bash
python3 <<'PY'
import json
path = 'grafana/weekly-dashboard.json'
with open(path) as f:
    d = json.load(f)
panel1 = next(p for p in d['dashboard']['panels'] if p['id'] == 1)
override = next(
    o for o in panel1['fieldConfig']['overrides']
    if o['matcher'].get('options') == 'difference_from_median'
)
new_thresholds = {
    "mode": "absolute",
    "steps": [
        {"color": "#5e7689", "value": None},   # baseline (anything below -10)
        {"color": "#bdcad4", "value": -10},
        {"color": "#ece5d6", "value": -3},
        {"color": "#d8a78a", "value": 3},
        {"color": "#974a3a", "value": 10},
    ],
}
new_cell = {"type": "color-background", "mode": "gradient"}
for prop in override['properties']:
    if prop['id'] == 'thresholds':
        prop['value'] = new_thresholds
    elif prop['id'] == 'custom.cellOptions':
        prop['value'] = new_cell
# If the cellOptions override wasn't present before, add it
ids = [p['id'] for p in override['properties']]
if 'custom.cellOptions' not in ids:
    override['properties'].append({'id': 'custom.cellOptions', 'value': new_cell})
if 'thresholds' not in ids:
    override['properties'].append({'id': 'thresholds', 'value': new_thresholds})
with open(path, 'w') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write('\n')
print("panel #1 diverging palette applied")
PY
```
Expected output: `panel #1 diverging palette applied`.

- [ ] **Step 2: Re-import the dashboard**

Run the re-import command. Expected: `"status":"success"`.

- [ ] **Step 3: Verify thresholds via API**

Run:
```bash
curl -s -u admin:admin http://127.0.0.1:3000/api/dashboards/uid/gym-weekly-display | \
  python3 -c "
import json,sys
panels = json.load(sys.stdin)['dashboard']['panels']
p1 = next(p for p in panels if p['id']==1)
ovr = next(o for o in p1['fieldConfig']['overrides'] if o['matcher']['options']=='difference_from_median')
thr = next(pr['value'] for pr in ovr['properties'] if pr['id']=='thresholds')
print('steps:', [(s.get('value'), s['color']) for s in thr['steps']])
"
```
Expected output:
```
steps: [(None, '#5e7689'), (-10, '#bdcad4'), (-3, '#ece5d6'), (3, '#d8a78a'), (10, '#974a3a')]
```

- [ ] **Step 4: Visual check**

Open http://localhost:3000/d/gym-weekly-display/gym-weekly-occupancy in a browser. The "Current vs Same Weekday/Time History" table panel should now show the `difference_from_median` cell with a Morandi color (linen/cream around 0, slate-blue for negative values, terracotta for ≥10). With current data both venues will show a small positive `diff` near `3`, so cells should appear in the warm-cream zone.

- [ ] **Step 5: Commit**

```bash
git add grafana/weekly-dashboard.json
git commit -m "feat(dashboard): apply Morandi diverging palette to comparison-table delta"
```

---

## Task 4: Convert Panel #2 (heatmap) to volkovlabs-echarts-panel

**Files:**
- Modify: `grafana/weekly-dashboard.json` — panel id 2: new SQL, new `type`, new `options.getOption` JS string

- [ ] **Step 1: Test the new SQL directly against Postgres for both granularities**

Run:
```bash
docker exec gym-postgres psql -U songhejun -d gym_fetch -c "
WITH bucketed AS (
  SELECT
    weekday_number,
    to_char(date_trunc('hour', ('2000-01-01 '||slot_start_time)::timestamp), 'HH24:00') AS slot_label,
    date_trunc('hour', ('2000-01-01 '||slot_start_time)::timestamp)::time AS slot_start_time,
    avg_current_count, avg_crowding_ratio, sample_count
  FROM weekly_occupancy_slots
  WHERE venue = '健身中心'
)
SELECT count(*) AS rows_60min, count(DISTINCT slot_label) AS slots_60min
FROM (SELECT weekday_number, slot_label FROM bucketed GROUP BY weekday_number, slot_label) sub;
"
```
Expected: `rows_60min` between 80–100 (varies with weekday open-hour mix), `slots_60min` = 14.

Then re-run the same query but without `date_trunc(...)` (keep `slot_label` and `slot_start_time` as-is) — that's the 30-min path. Verify `slots_30min` = 28.

- [ ] **Step 2: Replace panel #2 (SQL + type + getOption)**

The `getOption` JS is the entire function body. Write it once as a Python triple-quoted string, then store via the JSON serializer.

Run:
```bash
python3 <<'PY'
import json
path = 'grafana/weekly-dashboard.json'

new_sql = """WITH bucketed AS (
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
  AVG(avg_current_count)  AS avg_count,
  AVG(avg_crowding_ratio) AS crowding_ratio,
  SUM(sample_count)       AS sample_count
FROM bucketed
GROUP BY weekday_number, slot_label, slot_start_time
ORDER BY slot_start_time, weekday_number;"""

get_option = r"""const PALETTES = {
  terracotta: ['#ede4d4','#d8c4a0','#caa479','#b8754f','#974a3a','#642822'],
  sage:       ['#e8e7d6','#c9d2b3','#9eb288','#728c64','#4f6948','#2e4530'],
  slate:      ['#e6ebef','#bdcad4','#8ea1b1','#5e7689','#3d556a','#22344a'],
  rose:       ['#ece1de','#dbb9b9','#c08e95','#9a5e72','#6d3f56','#412236'],
  teal:       ['#dde7e5','#a8c2bf','#6fa097','#487a72','#2e5953','#1a3833'],
  lavender:   ['#e7e1ec','#c5b4d3','#a08abb','#7e639d','#5a417a','#3a2855'],
};

const series = data.series && data.series[0];
if (!series || !series.fields) return {};
const field = (n) => { const f = series.fields.find(x => x.name === n); return f ? f.values : null; };
const wd     = field('weekday_number');
const slot   = field('slot_label');
const cnt    = field('avg_count');
const ratio  = field('crowding_ratio');
const samp   = field('sample_count');
if (!wd || !slot || wd.length === 0) return {};

const paletteName = replaceVariables('${palette}');
const stops = PALETTES[paletteName] || PALETTES.slate;
const metric = replaceVariables('${metric}');

const DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];

const len = (v) => (typeof v.length === 'number' ? v.length : (v.toArray ? v.toArray().length : 0));
const at  = (v, i) => (typeof v.get === 'function' ? v.get(i) : v[i]);

const seen = new Set();
const yCats = [];
for (let i = 0; i < len(slot); i++) {
  const s = at(slot, i);
  if (!seen.has(s)) { seen.add(s); yCats.push(s); }
}

const heatData = [];
for (let i = 0; i < len(wd); i++) {
  const x = at(wd, i) - 1;
  const y = yCats.indexOf(at(slot, i));
  const c = Math.round(at(cnt, i));
  const r = Math.round(at(ratio, i) * 100);
  const s = at(samp, i);
  heatData.push([x, y, r, c, r + '%', s]);
}

const labelIdx = metric === 'crowding_ratio' ? 4 : 3;

return {
  grid:    { left: 60, right: 30, top: 40, bottom: 60, containLabel: true },
  xAxis:   { type: 'category', data: DAYS, position: 'top', axisLabel: { color: '#aaa' }, splitArea: { show: false } },
  yAxis:   { type: 'category', data: yCats, inverse: true, axisLabel: { color: '#aaa', fontSize: 10 } },
  visualMap: {
    min: 0, max: 100, calculable: true,
    orient: 'horizontal', left: 'center', bottom: 5,
    inRange: { color: stops },
    text: ['100%', '0%'],
    textStyle: { color: '#aaa' },
  },
  tooltip: {
    formatter: (p) => {
      const [x, y, r, c, _rp, s] = p.data;
      return DAYS[x] + ' ' + yCats[y] + '<br/>' +
             'avg_count: ' + c + ' 人<br/>' +
             'crowding_ratio: ' + r + '%<br/>' +
             'sample_count: ' + s + ' 週';
    },
  },
  series: [{
    type: 'heatmap',
    data: heatData,
    label: {
      show: true,
      formatter: (p) => String(p.data[labelIdx]),
      color: '#fff',
      textBorderColor: 'rgba(0,0,0,0.55)',
      textBorderWidth: 2,
      fontSize: 10,
    },
    emphasis: { itemStyle: { borderColor: '#fff', borderWidth: 1 } },
  }],
};
"""

with open(path) as f:
    d = json.load(f)

panel2 = next(p for p in d['dashboard']['panels'] if p['id'] == 2)
panel2['type'] = 'volkovlabs-echarts-panel'
panel2['targets'][0]['rawSql'] = new_sql
panel2['targets'][0]['format'] = 'table'
# Wipe table-panel-specific fieldConfig (visualMap on ECharts side handles colors)
panel2['fieldConfig'] = {'defaults': {}, 'overrides': []}
panel2['options'] = {
    'getOption': get_option,
    'renderer': 'canvas',
    'themeEditor': {'config': {}, 'name': 'default'},
    'visualEditor': {'code': '', 'codeEditor': 'svg', 'dataset': []},
}

with open(path, 'w') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write('\n')
print("panel #2 converted to ECharts heatmap")
PY
```
Expected output: `panel #2 converted to ECharts heatmap`.

- [ ] **Step 3: Re-import and verify panel type**

Run the re-import command, then:
```bash
curl -s -u admin:admin http://127.0.0.1:3000/api/dashboards/uid/gym-weekly-display | \
  python3 -c "import json,sys; p=next(x for x in json.load(sys.stdin)['dashboard']['panels'] if x['id']==2); print(p['type'], p['title'])"
```
Expected output: `volkovlabs-echarts-panel Weekly 30-Minute Occupancy Heatmap`.

- [ ] **Step 4: Test the panel SQL through the Grafana datasource proxy with each granularity**

Run:
```bash
for g in 60min 30min; do
  echo "--- granularity=$g ---"
  curl -s -u admin:admin -X POST -H 'Content-Type: application/json' http://127.0.0.1:3000/api/ds/query -d "{
    \"queries\": [{
      \"refId\": \"A\",
      \"datasource\": {\"type\":\"grafana-postgresql-datasource\",\"uid\":\"bfm1ctqr9jgn4b\"},
      \"format\": \"table\",
      \"rawSql\": \"WITH bucketed AS (SELECT weekday_number, CASE WHEN '$g'='60min' THEN to_char(date_trunc('hour', ('2000-01-01 '||slot_start_time)::timestamp), 'HH24:00') ELSE slot_label END AS slot_label, CASE WHEN '$g'='60min' THEN date_trunc('hour', ('2000-01-01 '||slot_start_time)::timestamp)::time ELSE slot_start_time END AS slot_start_time, avg_current_count, avg_crowding_ratio, sample_count FROM weekly_occupancy_slots WHERE venue = '健身中心') SELECT count(*) AS rows FROM (SELECT weekday_number, slot_label FROM bucketed GROUP BY weekday_number, slot_label) s;\"
    }]
  }" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['results']['A']['frames'][0]['data']['values'])"
done
```
Expected:
```
--- granularity=60min ---
[[<between 80 and 100>]]
--- granularity=30min ---
[[<between 160 and 200>]]
```

- [ ] **Step 5: Visual check — heatmap renders**

Open http://localhost:3000/d/gym-weekly-display/gym-weekly-occupancy. The middle panel should now be an ECharts heatmap with:
- 7 columns labeled Mon-Sun across the top
- 14 rows of time slots descending (08:00 at top, 21:00 at bottom) when `granularity=60min`
- Slate-blue gradient cell backgrounds
- Cell labels = integer counts (because `metric=avg_count` default)
- Transparent gaps for Sunday after 18:00 etc.
- Horizontal `visualMap` bar at the bottom showing the slate gradient with `0%` / `100%` ticks

Toggle `Granularity` to `30min` via the variable dropdown — heatmap should re-render with 28 rows. Toggle `Palette` to `rose` — colors should shift. Toggle `Metric` to `crowding_ratio` — cell labels should change to `XX%`.

If anything fails, the most likely cause is the `getOption` function not finding the right field shape; inspect the panel error in browser dev tools.

- [ ] **Step 6: Commit**

```bash
git add grafana/weekly-dashboard.json
git commit -m "feat(dashboard): convert weekly heatmap to ECharts with palette + granularity vars"
```

---

## Task 5: Convert Panel #3 (timeseries) to volkovlabs-echarts-panel line chart

**Files:**
- Modify: `grafana/weekly-dashboard.json` — panel id 3: change `type`, replace `fieldConfig`, add `options.getOption` JS string

- [ ] **Step 1: Replace panel #3 type + getOption**

Run:
```bash
python3 <<'PY'
import json
path = 'grafana/weekly-dashboard.json'

get_option = r"""const PALETTES = {
  terracotta: ['#ede4d4','#d8c4a0','#caa479','#b8754f','#974a3a','#642822'],
  sage:       ['#e8e7d6','#c9d2b3','#9eb288','#728c64','#4f6948','#2e4530'],
  slate:      ['#e6ebef','#bdcad4','#8ea1b1','#5e7689','#3d556a','#22344a'],
  rose:       ['#ece1de','#dbb9b9','#c08e95','#9a5e72','#6d3f56','#412236'],
  teal:       ['#dde7e5','#a8c2bf','#6fa097','#487a72','#2e5953','#1a3833'],
  lavender:   ['#e7e1ec','#c5b4d3','#a08abb','#7e639d','#5a417a','#3a2855'],
};

const series = data.series && data.series[0];
if (!series || !series.fields) return {};
const timeF = series.fields.find(f => f.type === 'time');
const valF  = series.fields.find(f => f.type === 'number');
if (!timeF || !valF) return {};

const len = (v) => (typeof v.length === 'number' ? v.length : (v.toArray ? v.toArray().length : 0));
const at  = (v, i) => (typeof v.get === 'function' ? v.get(i) : v[i]);

const lineColor = (PALETTES[replaceVariables('${palette}')] || PALETTES.slate)[5];

const points = [];
for (let i = 0; i < len(timeF.values); i++) {
  points.push([at(timeF.values, i), at(valF.values, i)]);
}

return {
  grid:    { left: 50, right: 20, top: 20, bottom: 30 },
  xAxis:   { type: 'time', axisLabel: { color: '#aaa' } },
  yAxis:   { type: 'value', axisLabel: { color: '#aaa' }, name: '人', nameTextStyle: { color: '#aaa' } },
  tooltip: { trigger: 'axis', formatter: (p) => p[0].axisValueLabel + '<br/>' + p[0].value[1] + ' 人' },
  series: [{
    type: 'line',
    data: points,
    showSymbol: false,
    lineStyle: { color: lineColor, width: 2 },
    itemStyle: { color: lineColor },
    areaStyle: { color: lineColor, opacity: 0.1 },
  }],
};
"""

with open(path) as f:
    d = json.load(f)

panel3 = next(p for p in d['dashboard']['panels'] if p['id'] == 3)
panel3['type'] = 'volkovlabs-echarts-panel'
panel3['fieldConfig'] = {'defaults': {}, 'overrides': []}
panel3['options'] = {
    'getOption': get_option,
    'renderer': 'canvas',
    'themeEditor': {'config': {}, 'name': 'default'},
    'visualEditor': {'code': '', 'codeEditor': 'svg', 'dataset': []},
}
# rawSql unchanged

with open(path, 'w') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write('\n')
print("panel #3 converted to ECharts line")
PY
```
Expected output: `panel #3 converted to ECharts line`.

- [ ] **Step 2: Re-import the dashboard**

Run the re-import command. Expected: `"status":"success"`.

- [ ] **Step 3: Verify panel type**

Run:
```bash
curl -s -u admin:admin http://127.0.0.1:3000/api/dashboards/uid/gym-weekly-display | \
  python3 -c "import json,sys; p=next(x for x in json.load(sys.stdin)['dashboard']['panels'] if x['id']==3); print(p['type'], p['title'])"
```
Expected output: `volkovlabs-echarts-panel Selected Venue Exact Counts`.

- [ ] **Step 4: Visual check**

Open the dashboard. The bottom panel should show a single dark-slate line (palette default) tracing `current_count` over the last 7 days, with a faint area fill beneath. Switch `Palette` to `terracotta` — line should turn dark brick-red. Switch back to `slate`.

- [ ] **Step 5: Commit**

```bash
git add grafana/weekly-dashboard.json
git commit -m "feat(dashboard): convert venue counts timeseries to ECharts with palette-driven color"
```

---

## Task 6: Update `README_Local.md` with the plugin install step

**Files:**
- Modify: `README_Local.md` — add a subsection after the existing "In Grafana, add a PostgreSQL datasource" section

- [ ] **Step 1: Find the insertion point**

Run:
```bash
grep -n 'curl -s -u admin:admin' README_Local.md
```
Expected: a single line number where the existing `curl ... /api/dashboards/db` import command lives.

- [ ] **Step 2: Insert the plugin requirement section right before that import command**

Use the Edit tool. The literal text to insert (verbatim, including the blank line at the end) is:

````
The weekly dashboard depends on the `volkovlabs-echarts-panel` plugin (Apache ECharts wrapper) for its heatmap and timeseries panels. Install it once into the running `gym-grafana` container:

```bash
docker exec gym-grafana grafana cli plugins install volkovlabs-echarts-panel
docker restart gym-grafana
```

Verify the plugin loaded:

```bash
curl -s -u admin:admin http://127.0.0.1:3000/api/plugins | \
  python3 -c "import json,sys; print(any(p['id']=='volkovlabs-echarts-panel' for p in json.load(sys.stdin)))"
```

Expected: `True`.

````

Insert it immediately above the line that begins with `If the datasource UID is still`, with one blank line separating the new block from that paragraph.

- [ ] **Step 3: Verify the change reads cleanly**

Run:
```bash
grep -B 1 -A 14 'depends on the .volkovlabs' README_Local.md
```
Expected: shows the inserted block immediately before the `If the datasource UID is still` paragraph.

- [ ] **Step 4: Commit**

```bash
git add README_Local.md
git commit -m "docs: note volkovlabs-echarts-panel requirement in local setup"
```

---

## Task 7: Update `CLAUDE.md` architecture note

**Files:**
- Modify: `CLAUDE.md` — the "Architecture notes that span multiple files" section, specifically the bullet about the hardcoded datasource UID

- [ ] **Step 1: Locate the existing bullet**

Run:
```bash
grep -n 'hardcoded datasource UID' CLAUDE.md
```
Expected: a single line number.

- [ ] **Step 2: Use the Edit tool to expand that bullet**

Replace the existing paragraph (which currently warns about the UID `bfm1ctqr9jgn4b`) with:

```markdown
**Grafana dashboard is wired to a hardcoded datasource UID.** `grafana/weekly-dashboard.json` references `bfm1ctqr9jgn4b`. If the local datasource has a different UID, the curl-based import will produce panels that can't query. The dashboard also requires the `volkovlabs-echarts-panel` plugin (for the heatmap and timeseries panels — see `README_Local.md`) and exposes two extra template variables, `${granularity}` (`30min`/`60min`, default `60min`) and `${palette}` (`terracotta`/`sage`/`slate`/`rose`/`teal`/`lavender`, default `slate`), that are read inside the ECharts panel JS to switch SQL bucketing and color stops at render time.
```

- [ ] **Step 3: Sanity check**

Run:
```bash
grep -A 1 'hardcoded datasource UID' CLAUDE.md | tail -1
```
Expected: includes the words `volkovlabs-echarts-panel` and `granularity` somewhere in the next line.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude.md): document new dashboard variables and echarts plugin dep"
```

---

## Task 8: End-to-end smoke test across all variable combinations

**Files:**
- None — verification only.

- [ ] **Step 1: Programmatic verification — every panel responds 200 with non-empty data**

Run:
```bash
python3 <<'PY'
import json, subprocess, sys

CURL = ['curl','-s','-u','admin:admin','-X','POST','-H','Content-Type: application/json','http://127.0.0.1:3000/api/ds/query']

def proxy(sql):
    body = json.dumps({"queries":[{"refId":"A","datasource":{"type":"grafana-postgresql-datasource","uid":"bfm1ctqr9jgn4b"},"format":"table","rawSql":sql}]})
    out = subprocess.run(CURL + ['-d', body], capture_output=True, text=True).stdout
    return json.loads(out)['results']['A']

# Heatmap SQL exercised at both granularities for both venues
venues = ['健身中心','室內游泳池']
results = []
for v in venues:
    for g in ['60min','30min']:
        sql = f"""WITH bucketed AS (SELECT weekday_number, CASE WHEN '{g}'='60min' THEN to_char(date_trunc('hour', ('2000-01-01 '||slot_start_time)::timestamp), 'HH24:00') ELSE slot_label END AS slot_label, slot_start_time, avg_current_count, avg_crowding_ratio, sample_count FROM weekly_occupancy_slots WHERE venue = '{v}') SELECT count(*) FROM (SELECT DISTINCT weekday_number, slot_label FROM bucketed) s;"""
        r = proxy(sql)
        rows = r['frames'][0]['data']['values'][0][0] if r.get('status')==200 else None
        results.append((v, g, r.get('status'), rows))
for r in results:
    print(r)
PY
```
Expected: 4 lines, each with `(<venue>, <granularity>, 200, <nonzero count>)`. No `None` rows. If any row is `None`, the SQL is broken — debug before moving on.

- [ ] **Step 2: Manual visual check matrix**

Open http://localhost:3000/d/gym-weekly-display/gym-weekly-occupancy in a browser. Walk this 4-axis check:

| Variable      | Values to step through                                  | What to check                                                                              |
| ------------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `venue`       | 健身中心 → 室內游泳池                                     | Heatmap row count and shape changes; panel #1 selected row colors stay sane                |
| `granularity` | 60min → 30min                                           | Heatmap row count switches 14 ↔ 28; line chart unaffected                                  |
| `palette`     | slate → terracotta → sage → rose → teal → lavender → slate | Heatmap colors and line color both shift; panel #1 `diff` cell does NOT change            |
| `metric`      | avg_count → crowding_ratio                              | Heatmap cell labels switch between integer counts and `XX%`; colors do not change          |

If any combination fails to render or renders incorrectly, treat it as a bug and return to the relevant earlier task.

- [ ] **Step 3: No code change → no commit.** Task 8 is verification only.

---

## File change summary

| File                                                                    | Tasks      | Net change                                                  |
| ----------------------------------------------------------------------- | ---------- | ----------------------------------------------------------- |
| `grafana/weekly-dashboard.json`                                         | 2, 3, 4, 5 | Add 2 template vars; rewrite panel #2 + #3; restyle panel #1 |
| `README_Local.md`                                                       | 6          | Add plugin install subsection                                |
| `CLAUDE.md`                                                             | 7          | Expand the datasource-UID architecture note                  |
| (no file)                                                               | 1, 8       | Operational + verification                                   |
