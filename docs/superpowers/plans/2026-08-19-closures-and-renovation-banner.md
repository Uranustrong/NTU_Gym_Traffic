# Closures Table 與整修 Banner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓休館期間（本次整修 + 每月第四個星期一）的讀數不再污染任何歷史統計，並讓公開頁面在休館時說明原因。

**Architecture:** 一張 `closures` 表加兩個 view。`closure_periods` 是休館規則的唯一定義（表中的一次性區間 ∪ `generate_series` 為兩個具名場館生成的每月第四個星期一）；`occupancy_excluding_closures` 供三處歷史統計使用。`gym_live` 直接對 `closure_periods` 加 `now()` 條件取出目前生效的區間。全部用 view 而非函式，因為 SQL 函式 body 的未限定名稱在 `SET search_path = ''` 的 SECURITY DEFINER 底下會解析失敗。

**Tech Stack:** PostgreSQL 17（Supabase）、純標準函式庫 Python、ECharts、GitHub Actions。無第三方 Python 套件。

**Spec:** [`docs/superpowers/specs/2026-08-19-closures-and-renovation-banner-design.md`](../specs/2026-08-19-closures-and-renovation-banner-design.md)

**Review:** 本計畫經 Codex 獨立審查兩輪——
round 1（`reviews/plan-round1.json`，18 findings / 6× P1）、
round 2（`reviews/plan-round2.json`，14 findings / 6× P1）。
第一版留存於 `.develop/2026-08-19-closures/plan-round1.md`。
兩輪都提出、而我兩輪都保留的項目有三個（`source` 欄位、`CLAUDE.md` 的架構
段落、seed 的 `DO UPDATE`），列在最後的 Self-Review 裡交由 GATE 1 決定。

## Global Constraints

- **時區一律 `Asia/Taipei`，永不繼承主機時區。**
- **每個 migration 檔案必須 idempotent**，重跑安全，且重跑不得改動 seed 以外的任何資料。
- **所有新物件必須能套進裸的 `postgres:17`**（無 `pg_cron`、`pg_net`、`anon`、`gym_reader`）。
- **`sql/grafana_weekly_views.sql` 內所有物件名稱一律不加 schema 限定**（`test_grafana_weekly_views.py` 會把整個檔案套進臨時 schema）。
- **`sql/migrations/005_public_api.sql` 內一律加 `public.` / `api_private.` 限定**（那裡的函式都是 `SET search_path = ''`）。
- **半開區間 `[starts_at, ends_at)`**，兩端皆 NOT NULL。
- **休館規則明列場館，不用「全館」萬用字元。** 公告寫的是「健身中心及溫水泳池」；讓規則自動套用到未來新增的場館，等於對沒驗證過的場館做出宣稱。
- **不改採集端**：`fetch_counts.py`、`sync_to_postgres.py` 一行都不動。
- **Task 0–7 一律不 commit。** 部署（Task 8）在使用者 commit 並 push 之後才執行——`publish.yml` checkout 的是遠端 ref，不是工作區。

---

## File Structure

| 檔案 | 責任 | Task |
|---|---|---|
| `sql/grafana_weekly_views.sql` | `closures` 表、seed、兩個新 view；兩個既有 view 改讀過濾後的來源 | 1, 2 |
| `test_grafana_weekly_views.py` | 上述的整合測試 | 1, 2 |
| `sql/migrations/003_roles_grants.sql` | `gym_reader` 對新物件的 SELECT | 3 |
| `sql/migrations/004_rls_policies.sql` | 新 view 的 invoker rights、對 `anon`/`authenticated` 的 REVOKE、`closures` 的 RLS + policy | 3 |
| `sql/migrations/005_public_api.sql` | `gym_panel1_meta_rows` 改來源；`gym_live` 回傳 `activeClosures` | 4 |
| `tools/public_template.html` | `activeClosures` 的接收、過期重濾與計時、`closureSpan()`、banner、`makeContext` 傳遞 | 5 |
| `test_public_page_js.py`（新） | `closureSpan()` 的兩種區間形狀，透過 node 執行 | 5 |
| `grafana/weekly-dashboard.json` | panel 4 的休館卡片（在 Grafana 端惰性） | 6 |
| `CLAUDE.md`、`docs/plan/2026-08-09-...md` | 記錄新的一層與 D1 完成 | 7 |

---

## Task 0: 準備一個 PostgreSQL 17 的測試資料庫

**Files:** 無（環境準備）

**Interfaces:**
- Produces: `TEST_POSTGRES_URL` 指得到的、已含 `public.occupancy` 的空資料庫

**不要用 `docker compose up -d postgres`。** `docker-compose.yml:3` 是 `postgres:18`，正式環境是 Supabase 的 **17.6**；拿 18 驗證 17 的相容性是白做。而且 compose 的 initdb 會自動套 001/002/005，讓 idempotent 驗證失去意義。

**絕對不可以**把 `TEST_POSTGRES_URL` 指向 Supabase——那些測試會 DROP schema 和 role。`dbtest_support.py` 會擋，不要繞過。

- [ ] **Step 1: 啟動 Docker daemon**

啟動 Docker Desktop 或 colima，然後 `docker ps` 應列出容器表頭而非 socket 錯誤。

- [ ] **Step 2: 起一個乾淨的 postgres:17**

```bash
docker run --rm -d --name gym-test-pg -p 5433:5432 \
  -e POSTGRES_USER=songhejun -e POSTGRES_DB=gym_fetch \
  -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17
sleep 5
psql postgresql://songhejun@127.0.0.1:5433/gym_fetch -X -At -c 'SHOW server_version;'
```

預期：`17.x`。

- [ ] **Step 3: 建立 `public.occupancy`**

`sql/grafana_weekly_views.sql` 會 `SELECT MIN(fetched_at) FROM occupancy`，所以直接套那個檔案到空資料庫會失敗。

```bash
psql postgresql://songhejun@127.0.0.1:5433/gym_fetch -X -v ON_ERROR_STOP=1 \
  -f sql/migrations/001_occupancy.sql
```

- [ ] **Step 4: 確認測試框架認得它**

```bash
TEST_POSTGRES_URL=postgresql://songhejun@127.0.0.1:5433/gym_fetch \
ALLOW_DESTRUCTIVE_DB_TESTS=1 \
python3 -m unittest test_grafana_weekly_views -v
```

預期：**2 個測試通過**（改動前的基準線）。顯示 skip 表示環境變數沒吃到，先解決。

---

## Task 1: closures 表與兩個 view

**Files:**
- Modify: `sql/grafana_weekly_views.sql`（插在檔案最前面）
- Test: `test_grafana_weekly_views.py`

**Interfaces:**
- Produces:
  - 表 `closures (venue text NOT NULL, starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL, reason text NOT NULL, source text NULL)`，主鍵 `(venue, starts_at)`
  - view `closure_periods (venue, starts_at, ends_at, reason, source)`，全欄位 NOT NULL 除 `source`
  - view `occupancy_excluding_closures`，欄位與 `occupancy` 相同

- [ ] **Step 1: 換掉 fixture 並寫失敗的測試**

`test_grafana_weekly_views.py` 的 `VALUES` 清單改成：

```python
            INSERT INTO occupancy (
                fetched_at,
                source_updated_at,
                venue,
                current_count,
                comfortable_count,
                capacity_count
            ) VALUES
                ('2026-05-04T08:05:00+08:00', NULL, '健身中心', 10, 40, 80),
                ('2026-05-11T08:20:00+08:00', NULL, '健身中心', 20, 40, 80),
                ('2026-05-18T08:35:00+08:00', NULL, '健身中心', 30, 40, 80),
                -- 2026-05-25 是五月的第四個星期一。以下兩列夾住 17:00 邊界。
                ('2026-05-25T08:45:00+08:00', NULL, '健身中心', 50, NULL, 100),
                ('2026-05-25T16:59:00+08:00', NULL, '健身中心', 777, 40, 80),
                ('2026-05-25T17:00:00+08:00', NULL, '健身中心', 888, 40, 80),
                ('2026-05-03T18:10:00+08:00', NULL, '健身中心', 99, 40, 80),
                ('2026-05-10T09:05:00+08:00', NULL, '室內游泳池', 12, NULL, 60),
                ('2026-05-17T09:20:00+08:00', NULL, '室內游泳池', 18, NULL, 60),
                -- 月休規則對兩個具名場館都生效。
                ('2026-05-25T10:00:00+08:00', NULL, '室內游泳池', 555, NULL, 60),
                -- 不在規則裡的第三個場館：月休不得波及它。這是 venue 從
                -- 「NULL = 全館」改成明列兩館的迴歸防線。
                ('2026-05-25T10:00:00+08:00', NULL, '壁球室', 7, NULL, 20),
                -- 場館範圍測試用：同一時刻兩個場館，只有被點名的該消失。
                ('2026-05-19T09:00:00+08:00', NULL, '室內游泳池', 61, NULL, 60),
                ('2026-05-19T09:00:00+08:00', NULL, '健身中心', 62, 40, 80);
```

**fixture 的場館名改成真實的 `健身中心` / `室內游泳池`**，因為月休規則現在明列場館名，用 `健身房` / `游泳池` 會測不到東西。

既有的 `test_weekly_slots_bucket_30_minutes_and_open_hours` 跟著改：

```python
    def test_weekly_slots_bucket_30_minutes_and_open_hours(self):
        output = self.run_fixture_query(
            """
            SELECT venue, weekday_number, slot_label, avg_current_count, median_current_count, sample_count, avg_crowding_ratio
            FROM weekly_occupancy_slots
            WHERE venue = '健身中心'
            ORDER BY weekday_number, slot_label;
            """
        )

        self.assertEqual(
            output.splitlines(),
            [
                "健身中心|1|08:00|15.00|15.00|2|0.3750",
                # 05-25 08:45 的 50 落在第四個星期一 17:00 前，已排除，
                # 這個桶只剩 05-18 的 30。
                "健身中心|1|08:30|30.00|30.00|1|0.3750",
                "健身中心|1|17:00|888.00|888.00|1|22.2000",
                # 05-19 是星期二。
                "健身中心|2|09:00|62.00|62.00|1|1.5500",
            ],
        )
```

`avg_crowding_ratio` 的值要用實際輸出校正（888/40 = 22.2，62/40 = 1.55），若不符先確認 `comfortable_count` 有沒有寫對。

**既有的第二個測試也會壞。** `test_current_vs_history_excludes_latest_row`
查的是 `健身房`，而 fixture 的場館名已經改掉了；它現在會拿到空字串而不是
`健身房|50|30.00|...`。它要驗的性質（最新一列不參與自己的歷史比較）在 Task 2
的新測試裡有更強的版本，所以**刪掉它**，並在 Task 2 補上取代品。刪除時在
commit 訊息裡說明，不要默默拿掉一個測試。

新增四個測試：

```python
    def test_fourth_monday_is_excluded_up_to_17_00(self):
        """第四個星期一 17:00 前不計入歷史，17:00 起計入。"""
        output = self.run_fixture_query(
            """
            SELECT to_char(fetched_at AT TIME ZONE 'Asia/Taipei', 'HH24:MI'), current_count
            FROM occupancy_excluding_closures
            WHERE venue = '健身中心'
              AND (fetched_at AT TIME ZONE 'Asia/Taipei')::date = DATE '2026-05-25'
            ORDER BY fetched_at;
            """
        )

        # 08:45 的 50 與 16:59 的 777 消失，17:00 的 888 留下。
        self.assertEqual(output.splitlines(), ["17:00|888"])

    def test_fourth_monday_applies_to_both_named_venues_only(self):
        """月休點名健身中心與室內游泳池；不在名單上的場館不受影響。"""
        output = self.run_fixture_query(
            """
            SELECT venue, count(*)
            FROM occupancy_excluding_closures
            WHERE (fetched_at AT TIME ZONE 'Asia/Taipei')::date = DATE '2026-05-25'
              AND (fetched_at AT TIME ZONE 'Asia/Taipei')::time < TIME '17:00'
            GROUP BY venue
            ORDER BY venue;
            """
        )

        # 健身中心 08:45/16:59 與室內游泳池 10:00 都被排除；壁球室 10:00 留下。
        self.assertEqual(output.splitlines(), ["壁球室|1"])

    def test_a_closure_row_only_hides_the_venue_it_names(self):
        """closures 表的列只影響它點名的場館。

        兩個場館在 2026-05-19 09:00 有完全相同的時間戳，closure 只點名
        室內游泳池——所以健身中心那列必須存活。少了這個同時刻的對照，
        測試會在「場館條件被完全忽略」時照樣通過。
        """
        output = self.run_fixture_query(
            """
            INSERT INTO closures (venue, starts_at, ends_at, reason)
            VALUES ('室內游泳池',
                    TIMESTAMPTZ '2026-05-19T00:00:00+08:00',
                    TIMESTAMPTZ '2026-05-20T00:00:00+08:00',
                    'Test closure');

            SELECT venue, current_count
            FROM occupancy_excluding_closures
            WHERE fetched_at = TIMESTAMPTZ '2026-05-19T09:00:00+08:00'
            ORDER BY venue;
            """
        )

        self.assertEqual(output.splitlines(), ["健身中心|62"])

    def test_the_renovation_seed_is_present_and_half_open(self):
        """seed 的整修區間含 9/20 當天、不含 9/21。"""
        output = self.run_fixture_query(
            """
            SELECT
                (SELECT count(*) FROM closure_periods
                 WHERE venue = '健身中心'
                   AND starts_at <= TIMESTAMPTZ '2026-09-20T23:59:00+08'
                   AND TIMESTAMPTZ '2026-09-20T23:59:00+08' < ends_at),
                (SELECT count(*) FROM closure_periods
                 WHERE venue = '健身中心'
                   AND starts_at <= TIMESTAMPTZ '2026-09-21T00:00:00+08'
                   AND TIMESTAMPTZ '2026-09-21T00:00:00+08' < ends_at);
            """
        )

        self.assertEqual(output, "1|0")
```

- [ ] **Step 2: 跑測試確認它失敗**

```bash
TEST_POSTGRES_URL=postgresql://songhejun@127.0.0.1:5433/gym_fetch \
ALLOW_DESTRUCTIVE_DB_TESTS=1 \
python3 -m unittest test_grafana_weekly_views -v
```

預期：**FAIL**，訊息含 `relation "occupancy_excluding_closures" does not exist` 或 `relation "closures" does not exist`。

- [ ] **Step 3: 寫實作**

在 `sql/grafana_weekly_views.sql` **最前面**插入：

```sql
-- ---------------------------------------------------------------------------
-- Closures -- when a venue was shut, so history can leave it out
-- ---------------------------------------------------------------------------
-- This file is named for the Grafana views it once held exclusively; it is now
-- also the bottom of the public API's stack. The closures table lives here
-- rather than in its own migration for one reason: weekly_occupancy_slots
-- depends on it, that view is 002, and a 001b_closures.sql would make
-- correctness depend on the shell's locale collation when apply_migrations.sh
-- expands `"$MIGRATIONS_DIR"/*.sql` -- which differs between a Mac and the
-- ubuntu runner that performs the weekly restore drill. Same file, no ordering
-- question.
--
-- Nothing here is schema-qualified, deliberately: test_grafana_weekly_views.py
-- applies this whole file into a throwaway schema via search_path, and a
-- hardcoded `public.` would write into the real one.

CREATE TABLE IF NOT EXISTS closures (
    venue     TEXT        NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    -- Half-open [starts_at, ends_at). NOT NULL: an open-ended closure sounds
    -- useful until you try to render it, and every closure this project has
    -- ever seen came with an end date. Add it when one does not.
    ends_at   TIMESTAMPTZ NOT NULL,
    -- Printed on the public page as-is, so English.
    reason    TEXT        NOT NULL,
    -- Where the claim came from. The banner renders it as a "(notice)" link,
    -- which is the only way a visitor can check the claim rather than trust it.
    source    TEXT,
    PRIMARY KEY (venue, starts_at),
    CONSTRAINT closures_range_ok CHECK (ends_at > starts_at)
);

-- DO UPDATE, not DO NOTHING -- the opposite of 005's rate_limit_policy seed,
-- and for the opposite reason. A rate limit is a tunable whose hand-edited
-- value is the correct one; a closure is a fact, and git should hold the only
-- copy. This also settles the backup question: backup.yml dumps only
-- public.occupancy, so closures is not in the dump -- but it is in version
-- control, and the restore drill rebuilds it from this file.
--
-- Consequence, stated rather than hidden: a hand-written UPDATE to these dates
-- is not in git, not in the backup, and will be overwritten the next time
-- apply_migrations.sh runs. Extending the renovation means editing this seed.
INSERT INTO closures (venue, starts_at, ends_at, reason, source) VALUES
    ('健身中心',
     TIMESTAMPTZ '2026-08-17 00:00:00+08',
     -- The announcement says "until 20 September", so the half-open range ends
     -- when the 21st begins.
     TIMESTAMPTZ '2026-09-21 00:00:00+08',
     'Renovation',
     'https://rent.pe.ntu.edu.tw/news/?K=157')
ON CONFLICT (venue, starts_at) DO UPDATE
    SET ends_at = EXCLUDED.ends_at,
        reason  = EXCLUDED.reason,
        source  = EXCLUDED.source;
-- One hazard this does NOT cover, and DO NOTHING would not either: the
-- conflict target is (venue, starts_at), so editing *those* fields in this
-- seed inserts a second row and leaves the old closure in place, silently
-- filtering a period nobody meant to filter. Changing a start date or a venue
-- therefore needs the old row named explicitly first:
--
--   DELETE FROM closures WHERE venue = '健身中心'
--                          AND starts_at = TIMESTAMPTZ '2026-08-17 00:00:00+08';
--
-- Changing only ends_at or reason is safe -- that is the common case (a
-- renovation slipping) and what DO UPDATE is here for.

-- The single definition of "was this venue closed at this instant". Two
-- consumers read it with different shapes -- occupancy_excluding_closures asks
-- per reading timestamp, gym_live asks about now() -- but neither restates the
-- rule.
--
-- Why the monthly rule is generated rather than stored: a (starts_at, ends_at)
-- table cannot express a recurrence, and materialising the next few years of
-- fourth Mondays as rows creates a trap where forgetting to extend them fails
-- silently. generate_series derives its range from the data itself plus now(),
-- so there is nothing to maintain and nothing to expire.
CREATE OR REPLACE VIEW closure_periods AS
WITH months AS (
    SELECT generate_series(
               date_trunc('month',
                   COALESCE((SELECT MIN(o.fetched_at) FROM occupancy o), now())
                   AT TIME ZONE 'Asia/Taipei'),
               date_trunc('month',
                   (now() AT TIME ZONE 'Asia/Taipei') + INTERVAL '1 month'),
               INTERVAL '1 month')::date AS month_start
),
fourth_mondays AS (
    -- Days to the month's first Monday, then three weeks on. ISODOW is 1 for
    -- Monday, so a month starting on Monday adds 0. The result always lands on
    -- the 22nd-28th, which is what "fourth Monday" means.
    SELECT month_start
           + ((8 - EXTRACT(ISODOW FROM month_start)::int) % 7)
           + 21 AS closed_on
    FROM months
),
-- Named, not a NULL wildcard. The notice says "健身中心及溫水泳池"; applying the
-- rule to whatever venue happens to appear in the data later would be a claim
-- about a venue nobody checked.
maintenance_venues (venue) AS (
    VALUES ('健身中心'::text), ('室內游泳池'::text)
)
SELECT venue, starts_at, ends_at, reason, source
FROM closures
UNION ALL
-- Posted in the site footer: "每個月第四個星期一為健身中心及溫水泳池休館日,
-- 17:00後開放". Confirmed against collected data: on 2026-05-25 the hourly peak
-- at 16:00 was 0 for 健身中心 and 4 for 室內游泳池, jumping to 121 and 29 at
-- 17:00. A normal Monday reads 76 and 20 in the same slots.
SELECT mv.venue,
       (fm.closed_on + TIME '00:00') AT TIME ZONE 'Asia/Taipei',
       (fm.closed_on + TIME '17:00') AT TIME ZONE 'Asia/Taipei',
       'Monthly maintenance'::text,
       'https://rent.pe.ntu.edu.tw/'::text
FROM fourth_mondays fm
CROSS JOIN maintenance_venues mv;

-- What every historical claim should read instead of `occupancy`.
--
-- `o.*` rather than a column list: CREATE OR REPLACE VIEW can append columns,
-- so re-running this file after occupancy gains one keeps the view in step
-- with no extra place to remember.
CREATE OR REPLACE VIEW occupancy_excluding_closures AS
SELECT o.*
FROM occupancy o
WHERE NOT EXISTS (
    SELECT 1
    FROM closure_periods c
    WHERE c.venue = o.venue
      AND o.fetched_at >= c.starts_at
      AND o.fetched_at <  c.ends_at
);
```

- [ ] **Step 4: 跑測試確認通過**

```bash
TEST_POSTGRES_URL=postgresql://songhejun@127.0.0.1:5433/gym_fetch \
ALLOW_DESTRUCTIVE_DB_TESTS=1 \
python3 -m unittest test_grafana_weekly_views -v
```

預期：**5 個測試全 PASS**（原有 2 個之中刪掉 1 個、改寫 1 個，加上新增 4 個）。

- [ ] **Step 5: 確認 idempotent，且重跑不動別的資料**

```bash
PSQL="psql postgresql://songhejun@127.0.0.1:5433/gym_fetch -X -v ON_ERROR_STOP=1"
$PSQL -f sql/grafana_weekly_views.sql
# 手動加一列 sentinel，模擬營運上另外插入的 closure
$PSQL -c "INSERT INTO closures (venue, starts_at, ends_at, reason)
          VALUES ('壁球室', TIMESTAMPTZ '2026-07-01 00:00+08',
                  TIMESTAMPTZ '2026-07-02 00:00+08', 'Sentinel')
          ON CONFLICT DO NOTHING;"
$PSQL -c "INSERT INTO occupancy (fetched_at, venue, current_count)
          VALUES (TIMESTAMPTZ '2026-07-01 10:00+08', '壁球室', 3)
          ON CONFLICT DO NOTHING;"
BEFORE=$($PSQL -At -c "SELECT md5(string_agg(venue||starts_at||ends_at||reason, '|' ORDER BY venue, starts_at)) FROM closures;")
COUNT_BEFORE=$($PSQL -At -c "SELECT count(*) FROM occupancy;")
$PSQL -f sql/grafana_weekly_views.sql
AFTER=$($PSQL -At -c "SELECT md5(string_agg(venue||starts_at||ends_at||reason, '|' ORDER BY venue, starts_at)) FROM closures;")
COUNT_AFTER=$($PSQL -At -c "SELECT count(*) FROM occupancy;")
echo "closures  $BEFORE -> $AFTER"
echo "occupancy $COUNT_BEFORE -> $COUNT_AFTER"
```

預期：兩對值各自相同（驗收 A11）。sentinel 那列**必須存活**——`DO UPDATE` 只針對 seed 自己那個主鍵，不會碰別人的列。

- [ ] **Step 6: 回報，不要 commit**

---

## Task 2: 讓兩個既有 view 讀過濾後的來源

**Files:**
- Modify: `sql/grafana_weekly_views.sql`（`weekly_occupancy_slots` 的 `localized` CTE、`current_vs_history` 的 `history` CTE）
- Test: `test_grafana_weekly_views.py`

**Interfaces:**
- Consumes: Task 1 的 `occupancy_excluding_closures`
- Produces: 兩個 view 欄位不變，語意變成「排除休館」

- [ ] **Step 1: 寫失敗的測試**

第一版計畫這裡寫了一個永遠紅不起來的測試（最新一列落在沒有歷史對照的 slot，改動前後都回空）。這版換成一個**確實會變**的斷言：讓最新一列有兩個同 slot 的歷史鄰居，其中一個在休館期間。

在 fixture 的 `VALUES` 尾端再加三列：

```python
                -- current_vs_history：最新一列是週二 09:00 的健身中心 62
                -- （已在 Task 1 的 fixture 裡）。給它兩個同 ISODOW=2、
                -- 同 09:00 slot 的歷史鄰居，一個開館一個休館。
                ('2026-05-05T09:00:00+08:00', NULL, '健身中心', 20, 40, 80),
                ('2026-05-12T09:00:00+08:00', NULL, '健身中心', 40, 40, 80),
                -- 週二不會是第四個星期一，所以用 closures 表製造休館。
                -- 這一列的 closure 在測試裡插入。
                ('2026-05-26T09:00:00+08:00', NULL, '健身中心', 999, 40, 80);
```

注意 `2026-05-26` 是五月最後一個週二，它會成為健身中心的最新一列——所以上面 Task 1 的 `test_weekly_slots_bucket_30_minutes_and_open_hours` 期望值要再加一列週二 09:00 的桶。**實作時以實際輸出為準修正這些期望值，但每一列都要能用 fixture 解釋。**

```python
    def test_current_vs_history_compares_against_open_days_only(self):
        """latest 側照實，history 側排除休館。

        最新一列是 05-26 09:00 的 999，同 ISODOW=2、同 09:00 slot 的歷史列有
        三筆：05-05 的 20、05-12 的 40、05-19 的 62。把 05-19 用 closure 蓋掉
        之後，中位數從 40 變成 30、樣本數從 3 變成 2。改動前這個測試會拿到
        40|3。
        """
        output = self.run_fixture_query(
            """
            INSERT INTO closures (venue, starts_at, ends_at, reason)
            VALUES ('健身中心',
                    TIMESTAMPTZ '2026-05-19T00:00:00+08:00',
                    TIMESTAMPTZ '2026-05-20T00:00:00+08:00',
                    'Test closure');

            SELECT current_count, historical_median_current_count, sample_count
            FROM current_vs_history
            WHERE venue = '健身中心';
            """
        )

        self.assertEqual(output, "999|30.00|2")
```

**這個測試在 `history` CTE 還讀 `occupancy` 時會拿到 `999|40.00|3`**，所以它真的會紅。

再加一個測試——上面那個在「實作者把 `latest` 也改成讀
`occupancy_excluding_closures`」時**照樣會過**，因為最新那列不在 closure 裡。
這一個專門擋那個錯誤，同時取代被刪掉的 `test_current_vs_history_excludes_latest_row`：

```python
    def test_the_latest_reading_is_shown_even_while_the_venue_is_closed(self):
        """`latest` 側不過濾，`history` 側過濾。

        把最新一列（05-26 09:00 的 999）本身也蓋進 closure。card 仍必須顯示
        999——休館期間感測器讀到什麼就顯示什麼——但中位數只能來自開館的歷史
        列。實作者若把 `latest` 也換成過濾後的來源，健身中心會整個從
        current_vs_history 消失，這個測試就會拿到空字串。
        """
        output = self.run_fixture_query(
            """
            INSERT INTO closures (venue, starts_at, ends_at, reason) VALUES
                ('健身中心', TIMESTAMPTZ '2026-05-19T00:00:00+08:00',
                             TIMESTAMPTZ '2026-05-20T00:00:00+08:00', 'Test closure'),
                ('健身中心', TIMESTAMPTZ '2026-05-26T00:00:00+08:00',
                             TIMESTAMPTZ '2026-05-27T00:00:00+08:00', 'Test closure');

            SELECT current_count, historical_median_current_count, sample_count
            FROM current_vs_history
            WHERE venue = '健身中心';
            """
        )

        self.assertEqual(output, "999|30.00|2")
```

- [ ] **Step 2: 跑測試確認它失敗，且失敗訊息是 `40.00|3`**

```bash
TEST_POSTGRES_URL=postgresql://songhejun@127.0.0.1:5433/gym_fetch \
ALLOW_DESTRUCTIVE_DB_TESTS=1 \
python3 -m unittest test_grafana_weekly_views.GrafanaWeeklyViewsTests.test_current_vs_history_compares_against_open_days_only -v
```

若失敗訊息不是 `999|40.00|3`，**先搞清楚為什麼**再往下——期望值算錯的測試比沒有
測試更糟。第二個測試在改動前會拿到 `999|40.00|3`（closure 還沒生效），改動後
`999|30.00|2`。

- [ ] **Step 3: 寫實作**

`weekly_occupancy_slots` 的 `localized` CTE：

```sql
    -- was: FROM occupancy
    FROM occupancy_excluding_closures
),
```

`current_vs_history` 的 `history` CTE，**只換 join 的那一側**：

```sql
    FROM latest_slotted l
    -- The `latest` CTE above deliberately still reads `occupancy`: it is the
    -- live number, and the card must show what the sensor says even while the
    -- venue is shut. Only the thing it is compared against excludes closures.
    JOIN occupancy_excluding_closures h
```

確認 `latest` CTE 仍是 `FROM occupancy`，一個字都不要改。

- [ ] **Step 4: 跑全部測試**

```bash
TEST_POSTGRES_URL=postgresql://songhejun@127.0.0.1:5433/gym_fetch \
ALLOW_DESTRUCTIVE_DB_TESTS=1 \
python3 -m unittest test_grafana_weekly_views -v
```

預期：全部 PASS。

- [ ] **Step 5: 未武裝時也要能跑**

```bash
python3 -m unittest discover -p 'test_*.py'
```

預期：全綠，DB 測試顯示 skip（驗收 A12）。

- [ ] **Step 6: 回報，不要 commit**

---

## Task 3: 權限——`gym_reader` 讀得到，`anon` / `authenticated` 讀不到

**Files:**
- Modify: `sql/migrations/003_roles_grants.sql`、`sql/migrations/004_rls_policies.sql`

**Interfaces:**
- Consumes: Task 1 的 `closures`、`closure_periods`、`occupancy_excluding_closures`

- [ ] **Step 1: 003 加 GRANT**

在 `GRANT SELECT ON public.current_vs_history TO gym_reader;` 之後：

```sql
-- The objects added with the closures table. All the views are
-- security_invoker (see 004), so Grafana reaching weekly_occupancy_slots now
-- also has to be able to read what that view reads.
GRANT SELECT ON public.closures TO gym_reader;
GRANT SELECT ON public.closure_periods TO gym_reader;
GRANT SELECT ON public.occupancy_excluding_closures TO gym_reader;
```

- [ ] **Step 2: 004 加 invoker rights、REVOKE 與 RLS**

在 `ALTER VIEW public.current_vs_history ...` 之後：

```sql
ALTER VIEW public.closure_periods              SET (security_invoker = true);
ALTER VIEW public.occupancy_excluding_closures SET (security_invoker = true);
```

在 `ALTER TABLE public.occupancy ENABLE ROW LEVEL SECURITY;` 之後：

```sql
-- Not for secrecy -- a closure is a public announcement. PostgREST exposes
-- every table and view in `public`, and an anon-readable one would be an
-- endpoint that never passes through api_private.rate_limit_check(). The two
-- SECURITY DEFINER wrappers are the only way in, and they are metered.
ALTER TABLE public.closures ENABLE ROW LEVEL SECURITY;
```

擴充既有 `DO $$` 區塊裡的 REVOKE 清單：

```sql
            EXECUTE format(
                'REVOKE ALL ON public.occupancy, '
                'public.weekly_occupancy_slots, '
                'public.current_vs_history, '
                'public.closures, '
                'public.closure_periods, '
                'public.occupancy_excluding_closures FROM %I', role_name);
```

檔案末尾加 policy：

```sql
DROP POLICY IF EXISTS gym_reader_closures_select ON public.closures;

-- gym_reader reaches closures only through the security_invoker views, but RLS
-- does not care how it got there.
CREATE POLICY gym_reader_closures_select ON public.closures
    FOR SELECT TO gym_reader USING (true);
```

- [ ] **Step 3: 裸資料庫（模擬還原演練）**

```bash
docker run --rm -d --name gym-bare-pg -p 5434:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres \
  -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17
sleep 5
tools/apply_migrations.sh --skip-roles postgresql://postgres@127.0.0.1:5434/postgres
```

預期：印出每個檔名與 `all migrations applied`，無錯誤（驗收 A10）。

- [ ] **Step 4: 完整路徑 + 權限驗證**

```bash
GYM_WRITER_PASSWORD=testpw GYM_READER_PASSWORD=testpw \
tools/apply_migrations.sh postgresql://postgres@127.0.0.1:5434/postgres

psql postgresql://postgres@127.0.0.1:5434/postgres -X -At -c "
SELECT relname, relrowsecurity FROM pg_class
WHERE relname IN ('occupancy','closures') AND relkind='r' ORDER BY relname;
SELECT relname, reloptions FROM pg_class
WHERE relname IN ('weekly_occupancy_slots','current_vs_history',
                  'closure_periods','occupancy_excluding_closures')
ORDER BY relname;"
```

預期：兩張表都 `t`；四個 view 的 `reloptions` 都含 `security_invoker=true`。

- [ ] **Step 5: 真的用 `gym_reader` 的身分把每個 Grafana 物件查一遍**

`has_table_privilege` 只證明「有 GRANT」，不證明 security_invoker 鏈路末端的 RLS 讓它讀得到。

```bash
psql postgresql://postgres@127.0.0.1:5434/postgres -X -At -c "
INSERT INTO occupancy (fetched_at, venue, current_count, comfortable_count, capacity_count)
VALUES (now() - interval '1 day', '健身中心', 5, 80, 161)
ON CONFLICT DO NOTHING;
SET ROLE gym_reader;
SELECT 'occupancy',                    count(*) FROM occupancy;
SELECT 'closures',                     count(*) FROM closures;
SELECT 'closure_periods',              count(*) FROM closure_periods;
SELECT 'occupancy_excluding_closures', count(*) FROM occupancy_excluding_closures;
SELECT 'weekly_occupancy_slots',       count(*) FROM weekly_occupancy_slots;
SELECT 'current_vs_history',           count(*) FROM current_vs_history;
RESET ROLE;"
```

預期：六行都成功回傳數字，沒有 `permission denied`。`closure_periods` 的數字應該 ≥ 2（seed 加上生成的月休）。

- [ ] **Step 6: 重跑一次確認 idempotent**

```bash
GYM_WRITER_PASSWORD=testpw GYM_READER_PASSWORD=testpw \
tools/apply_migrations.sh postgresql://postgres@127.0.0.1:5434/postgres
```

- [ ] **Step 7: 真正演練還原流程（R8 的核心那一條）**

Step 3 只跑了 `--skip-roles`，那不是 `backup.yml` 做的事——它還會把一份只含
`public.occupancy` 的 custom-format dump `pg_restore` 進去。新增一張帶 seed 的
表**有可能**和 `--data-only` 還原打架，這是唯一能證明沒有的方法。

```bash
# 來源：Task 0 那個有資料的容器。先塞幾列，否則 dump 沒有意義。
psql postgresql://songhejun@127.0.0.1:5433/gym_fetch -X -c "
INSERT INTO occupancy (fetched_at, venue, current_count)
SELECT now() - (n || ' hours')::interval, '健身中心', n
FROM generate_series(1, 50) n ON CONFLICT DO NOTHING;"
BEFORE=$(psql postgresql://songhejun@127.0.0.1:5433/gym_fetch -X -At -c 'SELECT count(*) FROM occupancy;')
pg_dump postgresql://songhejun@127.0.0.1:5433/gym_fetch \
  --format=custom --data-only --table=public.occupancy \
  --no-owner --no-privileges --enable-row-security -f /tmp/drill.dump

# 目標：全新空容器，只套 migration，再還原
docker run --rm -d --name gym-drill-pg -p 5436:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_DB=drill \
  -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17
sleep 5
DRILL=postgresql://postgres@127.0.0.1:5436/drill
tools/apply_migrations.sh --skip-roles "$DRILL"
pg_restore --data-only --no-owner --no-privileges -d "$DRILL" /tmp/drill.dump
AFTER=$(psql "$DRILL" -X -At -c 'SELECT count(*) FROM occupancy;')
echo "dumped $BEFORE, restored $AFTER"
psql "$DRILL" -X -At -c 'SELECT count(*) FROM closures;'
docker rm -f gym-drill-pg
```

預期：`$BEFORE` 等於 `$AFTER`，`pg_restore` 無錯誤，且 `closures` 是 **1** 列
（seed 從 migration 來，不從 dump 來）。

- [ ] **Step 8: 演練 docker-compose 的乾淨初始化路徑**

`docker-compose.yml` 的 initdb hook 只掛載 001 / 002 / 005——**不含 003 / 004**。
所以這條路徑上沒有 `gym_reader`、沒有 RLS，兩個 wrapper 必須照樣能跑。它也是
唯一會用到 `postgres:18` 的路徑。

```bash
docker compose down -v            # 清掉 volume，否則 initdb hook 不會觸發
docker compose up -d postgres
sleep 10
docker compose exec -T postgres psql -U songhejun -d gym_fetch -X -At -c "
INSERT INTO occupancy (fetched_at, venue, current_count, comfortable_count, capacity_count)
VALUES (now(), '健身中心', 5, 80, 161) ON CONFLICT DO NOTHING;
SELECT public.gym_heatmap() ? 'panel2';
SELECT public.gym_live(1) ? 'activeClosures';
SELECT count(*) FROM closures;"
```

預期：兩個 `t` 加一個 `1`。**`docker compose down -v` 會刪掉本機 Grafana 用的
那份資料**——如果那份資料還有用，先跳過這一步並在回報中註明。

- [ ] **Step 9: 清掉測試容器並回報，不要 commit**

```bash
docker rm -f gym-bare-pg
```

---

## Task 4: 005 的兩處改動

**Files:**
- Modify: `sql/migrations/005_public_api.sql`

**Interfaces:**
- Consumes: `public.occupancy_excluding_closures`、`public.closure_periods`
- Produces: `gym_live()` 的 JSON 多一個 `activeClosures` 陣列，元素為
  `{"venue": text, "from": timestamptz, "to": timestamptz, "reason": text, "source": text|null}`。
  `venue` 一定是具體場館名。

`gym_panel1_meta_rows` 沒有辦法用 `test_grafana_weekly_views.py` 覆蓋：005 全程 `public.` / `api_private.` 限定（因為它的函式都是 `SET search_path = ''`），塞不進臨時 schema。所以這裡改用容器裡的明確斷言，Step 4 就是 A4 對第三處歷史宣稱的驗收。

- [ ] **Step 1: `gym_panel1_meta_rows` 換來源**

```sql
    WITH today AS (
        SELECT EXTRACT(ISODOW FROM now() AT TIME ZONE 'Asia/Taipei')::int AS wd
    )
    SELECT count(DISTINCT (o.fetched_at AT TIME ZONE 'Asia/Taipei')::date)::int
    -- "based on N days" must not count a day the venue was shut.
    FROM public.occupancy_excluding_closures o, today
    WHERE o.venue = p_venue
      AND EXTRACT(ISODOW FROM o.fetched_at AT TIME ZONE 'Asia/Taipei')::int = today.wd;
```

- [ ] **Step 2: 加一個帶時刻參數的私有 helper**

在 `api_private.gym_is_open` 之後插入。這存在的理由是可驗證性：沒有它，A6 只能
「另外寫一個查 `closure_periods` 的平行查詢」，而那種檢查在 `gym_live` 不小心查
成 `closures`（因此永遠看不到每月休館）時**照樣會過**。

```sql
-- Parameterised so the recurring rule can be checked at a chosen instant --
-- gym_live calls it with no argument, the acceptance check calls it with a
-- fourth Monday. Same shape as gym_is_open(p_at) directly above.
--
-- api_private, not public: PostgREST cannot see this schema, so it costs no
-- REVOKE, no RLS decision and no A9 check. It is listed under internal_fns in
-- the privileges block below rather than api_fns -- Grafana never calls it.
CREATE OR REPLACE FUNCTION api_private.active_closure_rows(
    p_at timestamptz DEFAULT now())
RETURNS TABLE (venue text, starts_at timestamptz, ends_at timestamptz,
               reason text, source text)
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    SELECT c.venue, c.starts_at, c.ends_at, c.reason, c.source
    FROM public.closure_periods c
    WHERE c.starts_at <= p_at
      AND p_at < c.ends_at
    ORDER BY c.venue, c.starts_at;
$$;
```

在檔案底部權限區塊的 `internal_fns` 陣列裡加一行：

```sql
        'api_private.active_closure_rows(timestamptz)',
```

- [ ] **Step 3: `gym_live` 加 `activeClosures`**

在 `'freshnessExpected', api_private.gym_is_open(),` 之後插入：

```sql
            -- Drives the page's banner and the closed state on the live cards.
            -- It rides on gym_live rather than gym_heatmap because heatmap is
            -- cached an hour server-side and six hours in the browser -- a
            -- closure beginning or ending would take that long to show.
            -- Roughly 150 bytes; egress is not the constraint here.
            --
            -- Through api_private.active_closure_rows() rather than an
            -- inline subquery, and not through a view in `public`: the helper
            -- takes an instant, so the acceptance check can ask it about a
            -- fourth Monday and exercise this exact code path. A public view
            -- would have cost a REVOKE, an RLS decision and an A9 check for
            -- one call site.
            'activeClosures', coalesce((
                SELECT jsonb_agg(jsonb_build_object(
                           'venue',  c.venue,
                           'from',   c.starts_at,
                           'to',     c.ends_at,
                           'reason', c.reason,
                           'source', c.source))
                FROM api_private.active_closure_rows() c), '[]'::jsonb),
```

- [ ] **Step 4: 起容器並套完整 migration**

```bash
docker run --rm -d --name gym-api-pg -p 5435:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres \
  -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17
sleep 5
tools/apply_migrations.sh --skip-roles postgresql://postgres@127.0.0.1:5435/postgres
```

- [ ] **Step 5: 驗收 A4——`gym_panel1_meta_rows` 真的排除休館日**

`gym_panel1_meta_rows` 用 `now()` 的 ISODOW 過濾，所以固定日期的 fixture 只在
「今天剛好是那個星期幾」時才進得了函式。改用相對日期，並且**直接呼叫函式**
——去查 `occupancy_excluding_closures` 的話，就算 Task 4 Step 1 整個忘了改，
檢查也會過。

```bash
PSQL="psql postgresql://postgres@127.0.0.1:5435/postgres -X -At"
$PSQL -c "
WITH d AS (SELECT (now() AT TIME ZONE 'Asia/Taipei')::date AS today)
INSERT INTO occupancy (fetched_at, venue, current_count)
SELECT ((d.today - offs) + TIME '09:00') AT TIME ZONE 'Asia/Taipei', '健身中心', 40
FROM d, unnest(ARRAY[7, 14, 21]) AS offs
ON CONFLICT DO NOTHING;"
echo -n "before: "; $PSQL -c "SELECT n FROM api_private.gym_panel1_meta_rows('健身中心');"
$PSQL -c "
WITH d AS (SELECT (now() AT TIME ZONE 'Asia/Taipei')::date AS today)
INSERT INTO closures (venue, starts_at, ends_at, reason)
SELECT '健身中心',
       ((d.today - 14) + TIME '00:00') AT TIME ZONE 'Asia/Taipei',
       ((d.today - 13) + TIME '00:00') AT TIME ZONE 'Asia/Taipei',
       'Test closure'
FROM d ON CONFLICT DO NOTHING;"
echo -n "after:  "; $PSQL -c "SELECT n FROM api_private.gym_panel1_meta_rows('健身中心');"
```

預期：**`after` 必須恰好比 `before` 少 1**。不要斷言絕對值——今天若是星期一，
月休規則可能已經先排除掉其中一天，`before` 就會是 2 而不是 3。差值才是這裡要
證明的東西。`before` 等於 `after` 就是 Step 1 沒生效。


- [ ] **Step 6: 驗收 A5 / A6——`activeClosures`**

```bash
psql postgresql://postgres@127.0.0.1:5435/postgres -X -At -c "
INSERT INTO occupancy (fetched_at, venue, current_count, comfortable_count, capacity_count)
VALUES (now(), '健身中心', 5, 80, 161), (now(), '室內游泳池', 9, 50, 130)
ON CONFLICT DO NOTHING;
SELECT jsonb_pretty(public.gym_live(1) -> 'activeClosures');"
```

現在（2026-08-19）預期：一列健身中心的 `Renovation`。

A6 用指定時刻求值，且**必須走 `gym_live` 用的那個 helper**，不是另外寫一個查
`closure_periods` 的平行查詢——2026-08-24 是八月的第四個星期一：

```bash
psql postgresql://postgres@127.0.0.1:5435/postgres -X -A -F"|" -c "
SELECT venue, starts_at AT TIME ZONE 'Asia/Taipei',
               ends_at   AT TIME ZONE 'Asia/Taipei', reason
FROM api_private.active_closure_rows(TIMESTAMPTZ '2026-08-24 14:00+08');
SELECT '---';
SELECT venue, reason
FROM api_private.active_closure_rows(TIMESTAMPTZ '2026-08-24 17:30+08');"
```

預期：14:00 三列——健身中心的 `Monthly maintenance` 與 `Renovation`、室內游泳池
的 `Monthly maintenance`；17:30 只剩健身中心的 `Renovation`。

這同時證明了 `gym_live` 看得到生成的月休期間，而不只是 `closures` 表裡的列。


- [ ] **Step 7: 確認 `gym_heatmap` 沒被影響、005 可重跑**

```bash
psql postgresql://postgres@127.0.0.1:5435/postgres -X -At \
  -c "SELECT public.gym_heatmap() ? 'activeClosures';"
psql postgresql://postgres@127.0.0.1:5435/postgres -X -v ON_ERROR_STOP=1 \
  --single-transaction -f sql/migrations/005_public_api.sql
docker rm -f gym-api-pg
```

預期：第一個回 `f`（closure 只走 `gym_live`）；第二個無錯誤。

- [ ] **Step 8: 回報，不要 commit**

---

## Task 5: 頁面——接收、重濾、計時、banner

**Files:**
- Modify: `tools/public_template.html`
- Create: `test_public_page_js.py`

**Interfaces:**
- Consumes: Task 4 的 `gym_live().activeClosures`
- Produces:
  - `DATA.activeClosures`：陣列，元素為 `{venue（英文顯示名）, reason, source, from, to, span}`
  - `context.closures`：同一個陣列，Task 6 會讀
  - JS 函式 `closureSpan(fromIso, toIso) → string`，包在 `closure-span:begin/end` 標記之間供測試抽取

- [ ] **Step 1: 寫失敗的測試**

新檔 `test_public_page_js.py`：

```python
"""Unit tests for the closure-span formatter inside tools/public_template.html.

The template is a browser file, not an importable module, so the one piece of
it with real logic is fenced between marker comments and executed under node.
Skips when node is absent -- same shape as the database tests, which skip
rather than fail when unarmed.

Why this exists: the first version of this code subtracted 24 hours from every
half-open end, which renders the monthly maintenance window -- 24 Aug
00:00-17:00 -- as "24 Aug - 23 Aug".
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).with_name("tools").joinpath("public_template.html")
NODE = shutil.which("node")


def extract_block(name):
    text = TEMPLATE.read_text(encoding="utf-8")
    begin = text.index(f"// --- {name}:begin")
    end = text.index(f"// --- {name}:end")
    return text[begin:end]


@unittest.skipIf(NODE is None, "node not installed")
class ClosureSpanTests(unittest.TestCase):
    def span(self, from_iso, to_iso):
        script = (
            extract_block("closure-span")
            + f"\nprocess.stdout.write(closureSpan({json.dumps(from_iso)},"
              f" {json.dumps(to_iso)}));"
        )
        return subprocess.run([NODE, "-e", script], text=True,
                              capture_output=True, check=True).stdout

    def test_a_multi_day_range_names_the_last_closed_day(self):
        # Half-open to 21 Sep 00:00 means the 20th is the last closed day.
        self.assertEqual(
            self.span("2026-08-17T00:00:00+08:00", "2026-09-21T00:00:00+08:00"),
            "17 Aug – 20 Sep",
        )

    def test_a_single_whole_day_is_named_once(self):
        self.assertEqual(
            self.span("2026-08-17T00:00:00+08:00", "2026-08-18T00:00:00+08:00"),
            "17 Aug",
        )

    def test_a_part_day_range_keeps_its_clock_time(self):
        # The monthly maintenance morning. Subtracting a day here produced
        # "24 Aug – 23 Aug".
        self.assertEqual(
            self.span("2026-08-24T00:00:00+08:00", "2026-08-24T17:00:00+08:00"),
            "24 Aug · until 17:00",
        )

    def test_it_reads_the_range_in_taipei_not_the_host_timezone(self):
        # Same instants written in UTC. A host in UTC must still say 24 Aug.
        self.assertEqual(
            self.span("2026-08-23T16:00:00Z", "2026-08-24T09:00:00Z"),
            "24 Aug · until 17:00",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑測試確認它失敗**

```bash
python3 -m unittest test_public_page_js -v
```

預期：**ERROR**，`ValueError: substring not found`（標記還不存在）。

- [ ] **Step 3: 加 banner 的 CSS**

`<style>` 區塊 `#fatal { ... }` 之後：

```css
    /* Renovation / maintenance notice. The element itself is created and
       removed, not hidden -- an empty bordered box reads as "something is
       broken with the page". */
    .closure {
      margin: 14px 24px 0; padding: 10px 14px;
      border: 1px solid #4a3f22; border-left-width: 3px; border-radius: 4px;
      background: #191509; color: #d0a24c; font-size: 13px; line-height: 1.6;
    }
    .closure b { color: #e6c07b; font-weight: 500; }
    .closure a { color: #b8925a; }
```

- [ ] **Step 4: 加 DOM 掛載點**

`<div id="fatal"></div>` 之後加一個**空的錨點**，banner 元素本身會被插在它前面、移除時完全消失：

```html
  <div id="fatal"></div>
  <div id="closures-anchor" hidden></div>
```

- [ ] **Step 5: `closureSpan()` 與兩個 formatter**

在 `const statusEl = ...` 那個 `// ---- status ----` 區塊之前插入：

```javascript
    // ---- closure banner ---------------------------------------------------
    // --- closure-span:begin (extracted by test_public_page_js.py) ---
    // Month names come from a fixed array, not Intl's `month: "short"`. That
    // formatter returns "Sept" for September on current ICU builds and "Sep"
    // on older ones -- verified: node v24 with full ICU renders 21 September
    // as "21 Sept" -- so the banner's wording would depend on which browser
    // the viewer happens to have.
    const CLOSURE_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

    // Taipei wall-clock parts of an instant, whatever zone the viewer is in.
    // hourCycle "h23" rather than hour12:false: the latter leaves midnight as
    // "24" under some locale/ICU combinations, which would defeat the
    // whole-day test below.
    function taipeiParts(date) {
      const parts = {};
      for (const { type, value } of new Intl.DateTimeFormat("en-GB", {
        timeZone: "Asia/Taipei", year: "numeric", month: "numeric",
        day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
      }).formatToParts(date)) parts[type] = value;
      return { day: Number(parts.day), month: Number(parts.month),
               time: parts.hour + ":" + parts.minute };
    }

    function closureDay(parts) {
      return parts.day + " " + CLOSURE_MONTHS[parts.month - 1];
    }

    // Ranges are half-open [from, to). Two shapes, and they must not share a
    // formatter: a whole-day range ends on a local midnight, so the last day a
    // viewer would call "closed" is the day before it -- but the monthly
    // maintenance window ends at 17:00 the same day, and subtracting 24 hours
    // from that renders "24 Aug – 23 Aug".
    function closureSpan(fromIso, toIso) {
      const from = new Date(fromIso), to = new Date(toIso);
      const toParts = taipeiParts(to);
      if (toParts.time !== "00:00") {
        return closureDay(taipeiParts(from)) + " · until " + toParts.time;
      }
      const first = closureDay(taipeiParts(from));
      const last = closureDay(taipeiParts(new Date(to.getTime() - 86400000)));
      return first === last ? first : first + " – " + last;
    }
    // --- closure-span:end ---
```

- [ ] **Step 6: 跑測試確認通過**

```bash
python3 -m unittest test_public_page_js -v
```

預期：**4 個測試全 PASS**。

- [ ] **Step 7: 接收、重濾與計時**

`const DATA = {...}` 加兩個欄位：

```javascript
    const DATA = { venues: [], panel1History: {}, panel1Now: {}, panel2: {},
                   panel3: {}, panel4: null,
                   rawClosures: [], activeClosures: [] };
```

`applyLive` 改成：

```javascript
    function applyLive(p) {
      DATA.panel1Now = p.panel1Now || {};
      DATA.panel3 = p.panel3 || {};
      DATA.panel4 = p.panel4 ? translateField(p.panel4, "venue") : null;
      for (const v of Object.keys(DATA.panel3)) translateField(DATA.panel3[v], "metric");
      DATA.rawClosures = p.activeClosures || [];
      refilterClosures();
    }
```

在 `closure-span:end` 標記之後加：

```javascript
    // The payload is re-checked against the clock rather than trusted.
    // applyLive() is also called with the localStorage payload at boot, and
    // that can be days old -- a viewer returning on 21 September would
    // otherwise see the renovation banner until the first network reply.
    //
    // `span` is computed once here so the banner and panel 4 both print the
    // same string instead of formatting the dates twice.
    function refilterClosures() {
      const now = Date.now();
      DATA.activeClosures = DATA.rawClosures
        .filter((c) => new Date(c.from).getTime() <= now
                    && now < new Date(c.to).getTime())
        .map((c) => ({
          venue: display(c.venue),
          reason: c.reason,
          source: c.source,
          from: c.from,
          to: c.to,
          span: closureSpan(c.from, c.to),
        }));
      scheduleClosureExpiry();
    }

    // R4's boundary. Polling would eventually notice, but a tab left open
    // across 2026-09-21 00:00 keeps the banner for up to a full cycle -- and
    // indefinitely if the network is down, since a failed poll re-applies
    // nothing. `to` is already in hand, so the removal is scheduled from the
    // data rather than from the next successful request.
    //
    // Chained rather than one-shot: setTimeout saturates past ~24.8 days, so a
    // renovation ending five weeks out has to be re-armed rather than skipped.
    // The callback also re-checks the clock and reschedules if it fired early
    // -- timers are allowed to be a millisecond optimistic, and firing early
    // without this would leave the banner up until the next poll after all.
    //
    // The reverse direction (a closure *starting*) cannot be scheduled: the
    // API returns only what is active now, so that one waits for the poll.
    const MAX_TIMEOUT_MS = 2147483647;
    let closureTimer = null;
    function scheduleClosureExpiry() {
      if (closureTimer) { clearTimeout(closureTimer); closureTimer = null; }
      const now = Date.now();
      const next = DATA.activeClosures
        .map((c) => new Date(c.to).getTime())
        .filter((t) => t > now)
        .sort((a, b) => a - b)[0];
      if (next === undefined) return;
      closureTimer = setTimeout(() => {
        closureTimer = null;
        if (Date.now() < next) { scheduleClosureExpiry(); return; }
        refilterClosures();
        renderAll();
      }, Math.min(next - now, MAX_TIMEOUT_MS));
    }
```

- [ ] **Step 7b: 分頁重新可見時先同步重濾**

`refresh()` 是非同步的，而一個隱藏了三天的分頁重新顯示時，畫面上還掛著三天前
的 banner 直到網路回來。既有的 `visibilitychange` handler 改成：

```javascript
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        // Cheap and synchronous, so the stale banner is gone before the
        // network round trip rather than after it.
        refilterClosures();
        renderClosures();
        refresh();
      } else if (timer) { clearTimeout(timer); timer = null; }
    });
```

- [ ] **Step 8: 渲染 banner**

```javascript
    const closuresAnchor = document.getElementById("closures-anchor");

    function renderClosures() {
      // Remove the elements themselves rather than emptying a container: with
      // nothing closed there is no banner node in the document at all.
      for (const el of document.querySelectorAll(".closure")) el.remove();
      for (const c of DATA.activeClosures) {
        const div = document.createElement("div");
        div.className = "closure";
        const name = document.createElement("b");
        name.textContent = c.venue;
        div.appendChild(name);
        div.appendChild(document.createTextNode(
          " · " + c.reason + " · closed " + c.span + " "));
        if (c.source) {
          const a = document.createElement("a");
          a.href = c.source;
          a.rel = "noopener";
          a.textContent = "(notice)";
          div.appendChild(a);
        }
        closuresAnchor.parentNode.insertBefore(div, closuresAnchor);
      }
    }
```

**用 `createElement` 而不是 `innerHTML`**：`reason` 與 `venue` 來自資料庫，把使用者可見的字串直接串進 HTML 是個不該留下的習慣。

- [ ] **Step 9: 接到 `renderAll` 與 `makeContext`**

```javascript
    function renderAll() {
      renderClosures();
      Object.keys(PANEL_IDS).forEach(renderPanel);
    }
```

```javascript
    function makeContext(series) {
      return {
        panel: { data: { series: series ? [series] : [] } },
        // Public-page-only. Grafana builds its own context and has no such
        // field, so panel 4's JS sees undefined there and renders exactly as it
        // does today.
        closures: DATA.activeClosures,
        grafana: {
          replaceVariables: (s) => String(s)
            .split("${palette}").join(state.palette)
            .split("${venue}").join(display(state.venue))
            .split("${granularity}").join(state.granularity)
            .split("${metric}").join(state.metric),
        },
      };
    }
```

`renderClosures` 與 `refilterClosures` 都在 `renderAll` 之前定義，但 `scheduleClosureExpiry` 會呼叫 `renderAll`——JS 的函式宣告會提升，沒問題。

- [ ] **Step 10: 建置並在瀏覽器裡驗證五種狀態**

```bash
bash -c 'set -a; . ./supabase.secrets; set +a; \
  python3 tools/render_public_html.py --output-dir /tmp/gympage'
python3 -m http.server 8765 --directory /tmp/gympage
```

Task 4 尚未套到正式資料庫，所以線上還不會回 `activeClosures`。用 devtools console 直接餵資料驗證五種狀態（驗收 A7 / D-04）：

```javascript
// 1. 整修進行中
DATA.rawClosures = [{venue:"健身中心", reason:"Renovation",
  source:"https://rent.pe.ntu.edu.tw/news/?K=157",
  from:"2026-08-17T00:00:00+08:00", to:"2026-09-21T00:00:00+08:00"}];
refilterClosures(); renderAll();
document.querySelector(".closure").textContent
// 預期含 "Fitness Center"、"closed 17 Aug – 20 Sep"

// 2. 每月第四個星期一
DATA.rawClosures = [{venue:"健身中心", reason:"Monthly maintenance", source:null,
  from: new Date(Date.now()-3600e3).toISOString(),
  to:   new Date(Date.now()+3600e3).toISOString()}];
refilterClosures(); renderAll();
document.querySelector(".closure").textContent
// 預期含 "until"，且不得出現「結束日早於開始日」

// 3. 沒有 closure —— banner 完全不在 DOM
DATA.rawClosures = []; refilterClosures(); renderAll();
document.querySelectorAll(".closure").length   // 預期 0

// 4. 過期的快取 payload（模擬 9/21 之後回訪）
DATA.rawClosures = [{venue:"健身中心", reason:"Renovation", source:null,
  from:"2026-01-01T00:00:00+08:00", to:"2026-01-02T00:00:00+08:00"}];
refilterClosures(); renderAll();
document.querySelectorAll(".closure").length   // 預期 0

// 5. 兩個同時生效
DATA.rawClosures = [
  {venue:"健身中心", reason:"Renovation", source:null,
   from:"2026-08-17T00:00:00+08:00", to:"2026-09-21T00:00:00+08:00"},
  {venue:"室內游泳池", reason:"Monthly maintenance", source:null,
   from:new Date(Date.now()-3600e3).toISOString(),
   to:new Date(Date.now()+3600e3).toISOString()}];
refilterClosures(); renderAll();
document.querySelectorAll(".closure").length   // 預期 2
```

- [ ] **Step 11: 驗證消失計時器真的會開**

```javascript
DATA.rawClosures = [{venue:"健身中心", reason:"Renovation", source:null,
  from:new Date(Date.now()-1000).toISOString(),
  to:  new Date(Date.now()+5000).toISOString()}];
refilterClosures(); renderAll();
document.querySelectorAll(".closure").length   // 預期 1
setTimeout(() => console.log("after expiry:",
  document.querySelectorAll(".closure").length), 8000);   // 預期 0
```

- [ ] **Step 12: 跑 Python 測試**

```bash
python3 -m unittest test_render_public_html test_public_page_js -v
python3 -m unittest discover -p 'test_*.py'
```

- [ ] **Step 13: 回報，不要 commit**

---

## Task 6: panel 4 的休館卡片

**Files:**
- Modify: `grafana/weekly-dashboard.json`（**只改 `id: 4` 的 `options.getOption`**）

**Interfaces:**
- Consumes: Task 5 的 `context.closures`（元素含 `venue`、`reason`、`span`；Grafana 端為 `undefined`）
- Produces: 無下游

日期格式化不在這裡重寫——`span` 已經由頁面算好，這裡只印它。

- [ ] **Step 1: 抽出現有的 JS**

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("grafana/weekly-dashboard.json")
d = json.loads(p.read_text())
panel = next(x for x in d["panels"] if x["id"] == 4)
pathlib.Path("/tmp/panel4.js").write_text(panel["options"]["getOption"])
print("wrote /tmp/panel4.js", len(panel["options"]["getOption"]), "chars")
PY
```

- [ ] **Step 2: 改 `/tmp/panel4.js`**

在 `const paletteName = context.grafana.replaceVariables('${palette}');` 之前插入：

```javascript
// Closure lookup, keyed by the same display name the frame carries. Supplied
// only by the public page (tools/public_template.html builds this context);
// Grafana's own context has no such field, so this is an empty map there and
// every card below renders exactly as it always has. The date string is
// computed once by the page's closureSpan() and printed verbatim here rather
// than formatted a second time.
const closedBy = {};
for (const c of (context.closures || [])) closedBy[c.venue] = c;
```

找到 `const s = v.summary;` 到 `graphicEls.push(...)` 那一整段，改成：

```javascript
  const s = v.summary;
  const closure = closedBy[vname];
  const xCenterPct = isLeft ? '25%' : '75%';

  if (closure) {
    // A closed venue's live number is not wrong, but it is not occupancy
    // either -- during the 2026 renovation the counter sat at a flat 7-11 all
    // day, which is contractors badging in. Showing "8" beside "↓ 30 vs
    // same-slot median" would contradict the banner directly above it, so the
    // card says what is actually true instead.
    graphicEls.push(
      { type: 'text', left: xCenterPct, top: 12, style: { text: vname, fill: '#9aa', fontSize: 12, textAlign: 'center' } },
      { type: 'text', left: xCenterPct, top: 34, style: { text: 'CLOSED', fill: '#d0a24c', fontSize: 30, fontWeight: 'lighter', textAlign: 'center' } },
      { type: 'text', left: xCenterPct, top: 78, style: { text: closure.span, fill: '#888', fontSize: 11, textAlign: 'center' } },
      { type: 'text', left: xCenterPct, top: 94, style: { text: closure.reason, fill: '#888', fontSize: 12, textAlign: 'center' } },
    );
    return;
  }

  if (!s) return;
  const ratioPct = Math.round((s.ratio || 0) * 100);
  const bigCol = interpStops(stops, ratioPct);
  const cmfPct = s.comfortable ? Math.round((s.count / s.comfortable) * 100) + '%' : '—';
  const capPct = s.capacity ? Math.round((s.count / s.capacity) * 100) + '%' : '—';
  const subtitle = 'comfortable ' + cmfPct + ' · capacity ' + capPct;
  const diff = Number(s.diff || 0);
  const diffColor = diff > 0 ? stops[3] : (diff < 0 ? stops[1] : '#888');
  const arrow = diff > 0 ? '↑' : (diff < 0 ? '↓' : '–');
  const diffText = arrow + ' ' + Math.abs(diff).toFixed(0) + ' vs same-slot median';
  graphicEls.push(
    { type: 'text', left: xCenterPct, top: 12, style: { text: vname, fill: '#9aa', fontSize: 12, textAlign: 'center' } },
    { type: 'text', left: xCenterPct, top: 30, style: { text: String(s.count), fill: bigCol, fontSize: 40, fontWeight: 'lighter', textAlign: 'center' } },
    { type: 'text', left: xCenterPct, top: 78, style: { text: subtitle, fill: '#888', fontSize: 11, textAlign: 'center' } },
    { type: 'text', left: xCenterPct, top: 94, style: { text: diffText, fill: diffColor, fontSize: 12, textAlign: 'center' } },
  );
```

原本 `const xCenterPct = ...` 的那一行已經被提前，**原位置那一行必須刪掉**，否則 `SyntaxError: Identifier 'xCenterPct' has already been declared`。

- [ ] **Step 3: 語法檢查**

```bash
node --check <(echo "(function(context){"; cat /tmp/panel4.js; echo "})") \
  && echo "syntax OK"
```

fish 沒有 `<(...)`，改用：

```bash
bash -c 'printf "(function(context){\n"; cat /tmp/panel4.js; printf "\n})\n"' > /tmp/p4check.js
node --check /tmp/p4check.js && echo "syntax OK"
```

- [ ] **Step 4: 寫回 JSON，且只動那一個字串**

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("grafana/weekly-dashboard.json")
original = p.read_text()
d = json.loads(original)
panel = next(x for x in d["panels"] if x["id"] == 4)
old_js = panel["options"]["getOption"]
new_js = pathlib.Path("/tmp/panel4.js").read_text()
# Replace the encoded string in place rather than re-serialising the whole
# document: json.dumps would reformat every line and bury the real change in a
# whole-file diff.
old_enc = json.dumps(old_js, ensure_ascii=False)
new_enc = json.dumps(new_js, ensure_ascii=False)
assert original.count(old_enc) == 1, f"expected exactly one match, got {original.count(old_enc)}"
p.write_text(original.replace(old_enc, new_enc))
print("ok")
PY
```

- [ ] **Step 5: 確認 JSON 沒被改壞**

```bash
git diff --stat grafana/weekly-dashboard.json
python3 -c "
import json
d=json.load(open('grafana/weekly-dashboard.json'))
print('panels:', [p['id'] for p in d['panels']])
print('wrapper keys (must be empty):', set(d) & {'dashboard','overwrite'})
"
```

預期：diff 只有 1–2 行變動；`panels` 是 `[1, 2, 3, 4]`；wrapper 是空集合（file provisioning 要的是 bare dashboard model，不可以有 `{"dashboard": ...}` 外層）。

- [ ] **Step 6: 瀏覽器實測兩種狀態**

```bash
bash -c 'set -a; . ./supabase.secrets; set +a; \
  python3 tools/render_public_html.py --output-dir /tmp/gympage'
python3 -m http.server 8765 --directory /tmp/gympage
```

用 Task 5 Step 10 的第 1 段與第 5 段餵資料。預期：休館場館的卡片顯示 `CLOSED` / 日期 / 原因；未休館的照常顯示人數與 delta。然後：

```javascript
DATA.rawClosures = []; refilterClosures(); renderAll();
// 預期兩張卡片都回到人數 + delta，沒有 CLOSED
```

- [ ] **Step 7: 確認 Grafana 端沒變**

若本機 Grafana 有起來，等 10 秒讓 provisioning 重載，開 dashboard 確認 Live Cards 與改動前一致。沒起 Grafana 就在回報中明說沒有實測，並指出 `context.closures` 為 `undefined` 時 `closedBy` 為空物件、每張卡片都走原路徑。

- [ ] **Step 8: 回報，不要 commit**

---

## Task 7: 文件

**Files:** `CLAUDE.md`、`docs/plan/2026-08-09-github-actions-supabase-migration.md`

Codex 建議把這個 task 移到 follow-up。**保留在範圍內**：這個 repo 的 `CLAUDE.md` 是下一個 session 唯一的架構記憶，而這次改動新增了三個很容易被下一個人破壞的不變量（三處歷史過濾點、view 而非函式的理由、panel 4 的 JS 住在 dashboard JSON 裡）。它排在最後，所以寫的是實際 ship 出去的東西。

- [ ] **Step 1: `CLAUDE.md`**

在「**Opening-hours logic is duplicated in five places.**」那段之後加：

```markdown
**休館與開館是兩件事。** 開館時間是每週固定的時段；休館是「這段期間這個場館
關了」。後者住在 `closures` 表加兩個 view，定義在
`sql/grafana_weekly_views.sql`（= `002_views.sql`）的最前面。`closure_periods`
是唯一定義——表裡的一次性區間，聯集 `generate_series` 為**兩個具名場館**生成
的每月第四個星期一 00:00–17:00（資料證實：2026-05-25 兩館都在 17:00 才跳
起來）。`occupancy_excluding_closures` 是歷史統計該讀的東西。

- **規則明列場館，不用「NULL = 全館」。** 公告寫的是「健身中心及溫水泳池」；
  萬用字元會讓未來新增的場館自動繼承一條從沒對它驗證過的規則。
- **表為什麼在「views」檔案裡。** `weekly_occupancy_slots` 依賴它、而那個 view
  是 002；獨立成 `001b_closures.sql` 會讓正確性依賴 shell glob 的 locale
  collation，而還原演練跑在 ubuntu/glibc、開發在 macOS。
- **為什麼是 view 不是 `is_closed()` 函式。** SQL 函式 body 的未限定名稱是執行
  時依 caller 的 `search_path` 解析，而 `gym_heatmap()` 是 `SET search_path = ''`
  的 SECURITY DEFINER——函式在那裡會找不到 `closures`。View 在建立時就把依賴
  釘成 OID。
- **seed 用 `ON CONFLICT DO UPDATE`**，和 005 的 `rate_limit_policy` 相反：限流
  值是可調參數，closure 是事實，git 該是唯一真相。代價是手動 `UPDATE` 的日期
  不進 git、不進備份、下次套 migration 會被蓋掉。整修延期請改 seed。
  `backup.yml` 只 dump `public.occupancy`，closures 靠版本控制還原。
- **歷史統計有三處**，三處都要跟上：`weekly_occupancy_slots`、
  `current_vs_history` 的 join 側（`latest` 側刻意不濾，那是即時值）、
  `api_private.gym_panel1_meta_rows`。即時的三個（`gym_panel3_rows`、
  `gym_panel1_now_rows`、`gym_panel4_rows`）一律照實。
- **live card 的休館狀態住在 `grafana/weekly-dashboard.json` 的 panel 4 JS**，
  因為公開頁面的面板程式碼就是 `extract_panel_js()` 從那裡抽出來的。它讀
  `context.closures`——那是 `tools/public_template.html` 的 `makeContext()` 才有
  的欄位，Grafana 讀到 `undefined` 就走原路徑。所以檔案會變，Grafana 畫面不會。
- **日期字串由 `closureSpan()` 算一次**（`tools/public_template.html`，包在
  `closure-span:begin/end` 標記之間讓 `test_public_page_js.py` 抽出來測）。整日
  區間與當日區間的格式不同，共用一個公式會把每月休館渲染成
  「24 Aug – 23 Aug」。
```

- [ ] **Step 2: 標記 D1 完成**

`docs/plan/2026-08-09-github-actions-supabase-migration.md` 的 Phase D 第 1 項改成：

```markdown
1. ~~**第四個星期一過濾（只改 view 層）。**~~ **已完成 2026-08-19**，做法比原
   設計更進一步：直接建了計畫裡提到的「長久解」`closures` 表，第四個星期一與
   健身中心 2026-08-17～09-20 的整修停開一起入座。設計見
   [`docs/superpowers/specs/2026-08-19-closures-and-renovation-banner-design.md`](../superpowers/specs/2026-08-19-closures-and-renovation-banner-design.md)。
   與原設計的兩處差異：規則明列兩個場館而非套用到全部；過濾點是三處而非兩處
   （漏了 `api_private.gym_panel1_meta_rows`）。原始描述保留於下作為對照：
```

- [ ] **Step 3: 檢查文件裡的行號引用還對得上**

```bash
grep -n "render_public_html.py#L185" docs/plan/2026-08-09-github-actions-supabase-migration.md
```

失效就改成不帶行號的引用。

- [ ] **Step 4: 回報，不要 commit**

---

## ═══ GATE 2 ═══

**Task 0–7 完成、Codex 審過 diff、使用者 commit 並 push 之後，才執行 Task 8。**

`publish.yml` 由 GitHub Actions 執行，checkout 的是**已推送的 remote ref**，不是本機工作區。在 push 之前跑 `gh workflow run publish.yml` 只會重新部署一份沒有 banner 的舊頁面——同時資料庫已經回傳 `activeClosures`，A7/A8 會失敗而且原因看起來像程式碼壞掉。

---

## Task 8: 套用到正式 Supabase 並驗收（push 之後）

**Files:** 無（部署與驗證）

> **這個 task 會改動正式資料庫，必須先取得使用者明確同意。** 需要
> `supabase.secrets` 裡的 owner credential，那份憑證刻意不放在 GitHub。

- [ ] **Step 1: 確認前置條件**

```bash
git status --porcelain          # 必須是空的
git log --oneline -1
git rev-parse HEAD
git rev-parse origin/main       # 必須與上一行相同
```

任何一項不符就停下來——先完成 GATE 2。

- [ ] **Step 2: 量測改動前的基準**

```bash
bash -c 'set -a; . ./supabase.secrets; set +a; psql "$SUPABASE_OWNER_URL" -X -A -F"|" -c "
SELECT venue, round(avg(avg_current_count),2) AS mean_of_slots, sum(sample_count) AS samples
FROM weekly_occupancy_slots GROUP BY venue ORDER BY venue;"'
```

記下輸出，Step 5 要比對。

- [ ] **Step 3: 套用 migration**

**`supabase.secrets` 已經定義了 `apply_migrations.sh` 需要的三個變數本名**：
`PGPASSWORD`、`GYM_WRITER_PASSWORD`、`GYM_READER_PASSWORD`。`set -a` 之後它們
就在環境裡了，不要再賦值一次——寫成
`PGPASSWORD="$SUPABASE_OWNER_PASSWORD"` 會用一個不存在的變數把正確的值蓋成
空字串。

Port 也不用管：`supabase.secrets` 裡 `PGPORT=5432`，而 `SUPABASE_OWNER_URL`
刻意不帶 port，所以環境裡那個值就是實際生效的。（migration 是跨語句依賴 session
狀態的多語句操作，6543 的 transaction pooler 不保證——這正是那個檔案把 PGPORT
設成 5432 的原因。）

下面這一行就是 `supabase.secrets.example:10` 記載的呼叫方式，一字不改：

```bash
bash -c 'set -a; . ./supabase.secrets; set +a; tools/apply_migrations.sh "$SUPABASE_OWNER_URL"'
```

**部分失敗的處置**：`apply_migrations.sh` 對每個檔案用 `--single-transaction`，所以失敗的那個檔案整份回滾，但**它之前的檔案已經生效**。輸出會印出 `==> <檔名>`，最後一個印出來的就是失敗的那一份。因為每個檔案都 idempotent，處置方式是修好問題後**重跑整個腳本**，不要手動補那一份。中途停在 002 之後、005 之前是安全的：`gym_live` 還是舊版，只是熱區圖已經開始過濾——頁面會少一個 banner，不會壞。

- [ ] **Step 3b: 量測 `gym_live` 有沒有變慢**

`current_vs_history` 的 history 側從「直接 join `occupancy`」變成「join 一個帶
closure anti-join 的 view」，而 `gym_live` → `gym_panel4_series` →
`gym_panel4_rows` → `current_vs_history` 是**五分鐘路徑**。CLAUDE.md 已經把
shared CPU 與採集爭用列為這個專案的既有限制，所以這條路徑變重是要量的，不是
猜的。

先在**本機容器**（Task 4 那個，塞進代表性資料量）量，不要拿正式環境當測試場：

```bash
psql "$DRILL_OR_LOCAL" -X -c "
INSERT INTO occupancy (fetched_at, venue, current_count, comfortable_count, capacity_count)
SELECT now() - (n || ' minutes')::interval,
       CASE WHEN n % 2 = 0 THEN '健身中心' ELSE '室內游泳池' END,
       (random()*80)::int, 80, 161
FROM generate_series(1, 20000) n ON CONFLICT DO NOTHING;"

psql "$DRILL_OR_LOCAL" -X -c "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM current_vs_history;"
psql "$DRILL_OR_LOCAL" -X -c "\timing on" -c "SELECT length(public.gym_live(7)::text);" \
  -c "SELECT length(public.gym_live(7)::text);" \
  -c "SELECT length(public.gym_live(7)::text);"
```

改動前後各跑一次（改動前的數字用 `git stash` 或另一個容器取得）。

**門檻**：`gym_live(7)` 的第三次執行時間增加**不得超過 100 ms**。CLAUDE.md 記
載 `gym_live` 原本約 510 ms 一次、單執行緒迴圈就能吃掉七成的核心，而採集的
`COPY` 只有 175 ms 卻要跟它搶——再加一百毫秒已經是這條路徑能承受的上限。

超過門檻就**停下來回報**，不要自行加索引或改寫。可能的處置（給使用者決定）：
把 `closure_periods` 的月休生成改成物化、或讓 `current_vs_history` 只 anti-join
最近 N 週。這兩個都是設計變更，不是實作細節。

- [ ] **Step 4: 重建 heatmap 快取**

```bash
bash -c 'set -a; . ./supabase.secrets; set +a; psql "$SUPABASE_OWNER_URL" -X -At -c "
SELECT api_private.refresh_gym_heatmap_cache();"'
```

**不做這步，下一步會誤判成沒生效**——快取裡還是過濾前的 payload，最多會再服務一小時。

- [ ] **Step 5: 驗收 A1**

重跑 Step 2 的查詢。預期：健身中心的 `samples` 明顯下降（整修期間每天約 214 列，加上每月第四個星期一），`mean_of_slots` 回升；室內游泳池只少掉第四個星期一的部分。

- [ ] **Step 6: 驗收 A5**

```bash
bash -c 'set -a; . ./supabase.secrets; set +a; \
curl -s -X POST "$SUPABASE_URL/rest/v1/rpc/gym_live" \
  -H "apikey: $SUPABASE_PUBLISHABLE_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"p_days\":1}" | python3 -c "
import json,sys; print(json.dumps(json.load(sys.stdin)[\"activeClosures\"], ensure_ascii=False, indent=2))"'
```

預期：健身中心的 `Renovation` 一列，`from`/`to`/`reason`/`source` 齊全。

- [ ] **Step 7: 驗收 A9——`anon` 與 `authenticated` 都打不到**

```bash
bash -c 'set -a; . ./supabase.secrets; set +a; \
for t in closures closure_periods occupancy_excluding_closures weekly_occupancy_slots occupancy; do
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    "$SUPABASE_URL/rest/v1/$t?select=*&limit=1" -H "apikey: $SUPABASE_PUBLISHABLE_KEY")
  echo "$t: $code"
done'
```

預期：全部 401 / 403 / 404，**沒有任何一個回 200**。有 200 就立刻回頭檢查 004 的 REVOKE 清單。

`authenticated` 沒有可用的 token 可以直接打，改用資料庫端驗證：

```bash
bash -c 'set -a; . ./supabase.secrets; set +a; psql "$SUPABASE_OWNER_URL" -X -A -F"|" -c "
SELECT r.rolname, c.relname,
       has_table_privilege(r.rolname, c.oid, '\''SELECT'\'') AS can_select
FROM pg_class c
CROSS JOIN (SELECT rolname FROM pg_roles WHERE rolname IN ('\''anon'\'','\''authenticated'\'')) r
WHERE c.relname IN ('\''closures'\'','\''closure_periods'\'','\''occupancy_excluding_closures'\'')
ORDER BY r.rolname, c.relname;"'
```

預期：`can_select` 全部 `f`。

- [ ] **Step 8: 部署頁面並確認部署的是新的 commit**

```bash
gh workflow run publish.yml
sleep 20
gh run list --workflow=publish.yml --limit 1
gh run view --json headSha,status,conclusion --jq '.'
```

`headSha` 必須等於 Step 1 的 `git rev-parse HEAD`。等它 `success` 之後開
https://uranustrong.github.io/NTU_Gym_Traffic/

- [ ] **Step 9: 驗收 A7 / A8**

線上頁面預期：控制列下方一條整修 banner（`Fitness Center · Renovation · closed 17 Aug – 20 Sep (notice)`），panel 4 左邊卡片顯示 `CLOSED`，右邊 Indoor Pool 照常。

- [ ] **Step 10: 確認 Grafana 沒壞**

本機 Grafana 若有起來，開 dashboard 確認四個面板都還有資料——特別是熱區圖，`gym_reader` 若缺 GRANT 會整個空掉。

- [ ] **Step 11: 回報**

把 Step 2 與 Step 5 的數字並列、Step 7 的狀態碼、Step 8 的 `headSha` 一起交給使用者。

---

## Self-Review

**Spec coverage：**

| Spec 段落 | Task |
|---|---|
| closures 表與形狀 | 1 |
| `closure_periods`（含月休生成） | 1 |
| `occupancy_excluding_closures` | 1 |
| seed 與 `DO UPDATE` 取捨 | 1 |
| 檔案落點（全部在 002） | 1 |
| 三處歷史過濾點 | 2（前兩處）、4（`gym_panel1_meta_rows`） |
| `gym_live` 的 `activeClosures` | 4 |
| banner | 5 |
| live card 休館狀態 | 6 |
| 為什麼 `weekly-dashboard.json` 躲不掉 | 6 |
| 權限（003 / 004） | 3 |
| 套用後重建 heatmap 快取 | 8 Step 4 |
| 測試 | 1、2、5 |

**相對於第一版計畫的三處變更**（spec 已同步更新成現況，所以這些不再是與 spec
的偏離；列在這裡是為了讓 GATE 1 看得到改了什麼）：

1. **`active_closures` view 被砍掉**，`gym_live` 直接對 `closure_periods` 加
   `now()` 條件。spec 寫的是三個 view，實際是兩個。Codex E-02：一個呼叫點不值
   得在 `public` 多一個物件（每個都要付 REVOKE、RLS 決策與驗證步驟）。
2. **`venue` 從 nullable 改成 NOT NULL，月休明列兩個場館。** spec 寫的是
   `NULL = 全館`。這比 YAGNI 更嚴重：公告只點名兩館，萬用字元會讓未來新增的
   場館自動繼承一條從沒驗證過的規則。`ends_at` 與 `id` 也一併收掉。
3. **`source` 欄位保留**（Codex 建議一併砍）。它有真實 consumer：banner 的
   `(notice)` 連結是訪客查證公告的唯一途徑。

**新增、spec 未寫的兩件事：**

1. **客戶端重濾 + 消失計時器**（Task 5 Step 7）。`applyLive()` 也會被
   localStorage 的舊 payload 呼叫，且開著的分頁跨過結束時刻時輪詢最多慢五分
   鐘。服務需求 R4，成本約 25 行。
2. **`test_public_page_js.py`**（Task 5 Step 1）。這個 repo 沒有 JS 測試設施；
   新檔只用 `node -e` 跑一段被標記夾住的純函式，沒有 node 就 skip——和 DB 測試
   的 skip 模式一致，不引入任何相依。第一版計畫的日期公式會渲染出
   「24 Aug – 23 Aug」，這正是它要擋的東西。
3. **`api_private.active_closure_rows(p_at)`**（Task 4 Step 2，round 2 的
   D-04）。round 1 砍掉了 `active_closures` **view**，這個是不同的東西：它在
   `api_private`（PostgREST 看不見，不用 REVOKE、不用 RLS、不進 A9），而且帶
   時刻參數——沒有它，A6 只能寫一個平行查詢，那在 `gym_live` 查錯物件時照樣
   會過。形狀比照既有的 `gym_is_open(p_at)`。
4. **`gym_live` 的效能門檻**（Task 8 Step 3b，round 2 的 C-01）。
   `current_vs_history` 變重而它在五分鐘路徑上；CLAUDE.md 把 shared CPU 與採集
   爭用列為既有限制，所以設了「第三次執行不得多於 +100 ms」的硬門檻，超過就
   停下來回報而不是自行最佳化。

**三個與 Codex 持續分歧的項目（GATE 1 決定）：**

| 項目 | Codex 立場 | 我的立場 |
|---|---|---|
| `source` 欄位與 `(notice)` 連結 | A5 沒要求，移到 follow-up | 保留。它是訪客查證公告的唯一途徑，而 R3 要求的是「說明休館」——一則無法查證的說明是比較弱的說明 |
| `CLAUDE.md` 架構段落 | 純一致性工作，移到 follow-up | 保留。這個 repo 的 `CLAUDE.md` 是下一個 session 唯一的架構記憶，而這次新增了三個很容易被誤改的不變量 |
| seed 用 `ON CONFLICT DO UPDATE` | 改 `DO NOTHING`，避免蓋掉營運上的緊急修改 | 保留（使用者已決定）。但 round 2 揭露了一個真實缺陷，兩種寫法都有：conflict key 是 `(venue, starts_at)`，所以改動**那兩個欄位**會插入第二列並留下舊的孤兒列。已在 seed 註解裡寫上必要的 `DELETE` 語句 |

**Placeholder 掃描：** 無 TBD / TODO / 「類似 Task N」。Task 1 Step 1 與 Task 2
Step 1 有「以實際輸出為準修正期望值」的指示，但都附帶「每一列都要能用 fixture
解釋」的約束，且 Task 2 Step 2 明確要求先確認紅色訊息是 `999|40.00|3`——不允許
為了變綠而放寬斷言。

**型別一致性：** `activeClosures` 元素的鍵在 Task 4（SQL）、Task 5
（`refilterClosures`）、Task 6（`closedBy[c.venue]`、`closure.span`、
`closure.reason`）三處一致。`context.closures` 的名稱在 Task 5 Step 9 與 Task 6
Step 2 一致。`closureSpan` 的名稱在 Task 5 Step 5（定義）、Step 7（呼叫）與
`test_public_page_js.py` 三處一致。

---

## Execution Handoff

本計畫走 `/develop` 流程：先進 GATE 1 由使用者批准，實作後經 Codex 審 diff，
再進 GATE 2 由使用者 commit。Task 8 在 GATE 2 之後才執行。
