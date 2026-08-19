# Closures table 與整修 banner — 設計

**日期**：2026-08-19
**分類**：architectural（新子系統，橫跨 002 / 003 / 004 / 005 與公開頁面，
改變 `weekly_occupancy_slots` 的語意）
**需求**：[`.develop/2026-08-19-closures/requirements.md`](../../../.develop/2026-08-19-closures/requirements.md)

---

## 問題

健身中心 2026-08-17 至 09-20 整修停開（公告
[`/news/?K=157`](https://rent.pe.ntu.edu.tw/news/?K=157)，2026-07-29 發布）。
採集端照常運作，於是每天約 214 列「幾乎沒人」的讀數正在寫進 `occupancy`。

熱區圖與 Popular Times 走 `weekly_occupancy_slots`，**全歷史平均、沒有任何
時間過濾**。健身中心目前 9,493 列、全歷史平均 42.5。整修 35 天約產生 7,500
列接近 0 的讀數，到 9/20 會佔全歷史約 44%，把平均壓到 25 上下——看起來會像
健身中心整體變冷清，而不是關了一個月。

同一個缺口還讓另外兩處說謊：`current_vs_history`（live card 的「vs 歷史中位
數」）直接 join `occupancy`，完全繞過 view；`gym_panel1_meta_rows` 的
「based on N days」會把休館日算成有資料的一天。

而且這不是單一事件。網站頁尾載明「每個月第四個星期一為健身中心及溫水泳池休館
日，17:00後開放」，這條規則從專案開始就一直在污染資料，只是每月十小時、量級
小到沒人注意。2026-08-09 的遷移計畫把它列為 follow-up D1，並且已經指出長久解
是一張 closures table。整修把「小到可以拖」變成「大到不能拖」，也讓兩者值得
一起解。

---

## 設計

### 資料模型：一張表、兩個 view

```
closures                        表。一次性、有明確起訖的休館。整修是第一列。
    │
    ▼
closure_periods                 view。closures ∪ generate_series 為兩個具名
    │                           場館生成的每月第四個星期一 00:00–17:00
    ├──────────────────────────────┐
    ▼                              ▼
occupancy_excluding_closures    gym_live() 裡的一段子查詢
  occupancy 減去落在任一            closure_periods 中涵蓋 now() 的那些
  closure_period 內的列             → JSON 的 activeClosures
    │                              │
    ▼                              ▼
  歷史統計                        banner + card 休館狀態
```

`closure_periods` 是休館規則的**唯一定義**（需求 R5）。兩個下游用途形狀不同
——一個按每列的時間戳過濾歷史，一個按 `now()` 回答「現在誰休館」——但兩者
讀的是同一份定義。

### 為什麼是 view，不是 `is_closed()` 函式

直覺做法是寫一個 `is_closed(venue, at)` 函式讓各處呼叫。這在本專案會壞掉：

SQL 函式 body 裡的未限定名稱是**執行時**依 caller 的 `search_path` 解析，而
`public.gym_heatmap()` 是 `SET search_path = ''` 的 SECURITY DEFINER 函式。
從那裡呼叫下來，函式就找不到 `closures`。若改成寫死 `public.closures`，則
`test_grafana_weekly_views.py`（把整個檔案套進臨時 schema）會被污染到真正的
`public`。

View 沒有這個問題：view 在建立時就把依賴解析成 OID 並記錄成 catalog 相依，
執行時的 `search_path` 完全不影響。既有的 `weekly_occupancy_slots` 之所以能
在 SECURITY DEFINER 底下正確運作，靠的正是這一點。

### 為什麼每月規則不進表

`closures` 的形狀是 `(starts_at, ends_at)` 區間，表達不了「每月第四個星期
一」這種循環。三條路，選第三條：

1. 加一個 `recurrence` 欄位做迷你規則引擎——為了一條規則不值得。
2. 把未來幾年的第四個星期一實體化成列——會產生「忘了續期就靜默失效」的陷阱，
   而且失效時沒有任何訊號。
3. **在 `closure_periods` 裡用 `generate_series` 生成**：從最早一筆資料的月份
   長到下個月為止。範圍由資料本身和 `now()` 決定，不需要維護，也不會過期。

生成的區間是每月第 4 個星期一（必落在該月 22–28 日）的當地時間 00:00 至
17:00，**為兩個具名場館各生成一列**：健身中心、室內游泳池。

不用「`venue IS NULL` = 全館」的萬用字元，這點是刻意的。公告寫的是「健身中心
及溫水泳池」，而萬用字元會讓未來新增的任何場館自動繼承一條從沒對它驗證過的
規則——預設值的方向錯了。

### 表的形狀

```sql
CREATE TABLE IF NOT EXISTS closures (
    venue     TEXT        NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at   TIMESTAMPTZ NOT NULL,
    reason    TEXT        NOT NULL,  -- 頁面直接顯示，所以存英文
    source    TEXT,                  -- 公告網址，banner 的 (notice) 連結
    PRIMARY KEY (venue, starts_at),
    CONSTRAINT closures_range_ok CHECK (ends_at > starts_at)
);
```

區間語意是**半開** `[starts_at, ends_at)`。整修那列是
`2026-08-17 00:00+08` → `2026-09-21 00:00+08`，所以「至 9 月 20 日」含當天，
而 9/21 零時自動恢復（需求 R4）。

沒有代理主鍵、沒有可為 NULL 的 `ends_at`：`(venue, starts_at)` 本來就是自然
鍵，而「尚未宣布結束」的休館聽起來有用，直到要把它渲染出來為止——這個專案
見過的每一次休館都帶結束日期。真的出現無限期休館時再加。

`source` 保留，因為它有真實的消費者：banner 的 `(notice)` 連結是訪客查證公告
的唯一途徑。

### Seed 用 `DO UPDATE`，和 005 相反

`005_public_api.sql` 的 `rate_limit_policy` 用 `ON CONFLICT DO NOTHING`，
刻意保護手調的限流值。closures 的答案相反：**用 `ON CONFLICT DO UPDATE`**。

理由是兩種資料的性質不同。限流值是可調參數，手調的那份才是對的；closure 是
事實，git 才該是唯一真相。這也順帶解掉備份問題——`backup.yml` 只 dump
`public.occupancy`，closures 不在備份裡，但它的內容在版本控制中，還原演練會
從 migration 重建。

**代價寫在這裡，不藏起來**：手動 `UPDATE` 改的日期不會進 git、不會進備份，
下一次 `apply_migrations.sh` 會把它蓋回去。整修延期的正確做法是改 migration
的 seed 再重新套用。

### 檔案落點：全部放進 `sql/grafana_weekly_views.sql`

closures 的 DDL、兩個 view、以及 seed 全部放在
[`sql/grafana_weekly_views.sql`](../../../sql/grafana_weekly_views.sql)（即
`sql/migrations/002_views.sql` 這個 symlink 指向的檔案）的頂端，排在兩個既有
view 之前。

原因是順序。`weekly_occupancy_slots` 在 002，而 closures 必須在它之前存在。
另開一個 `001b_closures.sql` 會讓正確性依賴 shell glob 的 locale collation
——macOS 上排序正確，但 `backup.yml` 的還原演練跑在 ubuntu/glibc 上，那裡的
`_` 與 `b` 比較規則不同。同一個檔案裡就沒有這個問題，而且：

- `test_grafana_weekly_views.py` 維持「套一個檔案」的結構不變
- `docker-compose.yml` 的三個 initdb 掛載不用增減
- `tools/apply_migrations.sh` 不用改
- 不需要重新編號 003 / 004 / 005

代價是一個叫 `grafana_weekly_views.sql` 的檔案裡有一張 base table。用檔頭註解
說明——這個檔名其實早就名不副實，它同時是公開 API 的底層。

### 過濾點：三處歷史宣稱

| 位置 | 檔案 | 改法 |
|---|---|---|
| `weekly_occupancy_slots` | 002 | `localized` CTE 改讀 `occupancy_excluding_closures` |
| `current_vs_history` | 002 | `history` CTE 的 join 改讀同一個 view；`latest` CTE **不改**（它是即時值） |
| `api_private.gym_panel1_meta_rows` | 005 | 改讀 `public.occupancy_excluding_closures` |

**不改**（需求 R2）：`gym_panel3_rows`、`gym_panel1_now_rows`、
`gym_panel4_rows`。原始讀數照實呈現。

### API：`gym_live` 多一個 `activeClosures`

```json
{
  "dataAsOf": "...", "servedAt": "...", "freshnessExpected": true,
  "activeClosures": [
    {"venue": "健身中心", "from": "2026-08-17T00:00:00+08:00",
     "to": "2026-09-21T00:00:00+08:00", "reason": "Renovation",
     "source": "https://rent.pe.ntu.edu.tw/news/?K=157"}
  ],
  "panel1Now": {...}, "panel3": {...}, "panel4": {...}
}
```

放在 `gym_live` 而不是 `gym_heatmap`：heatmap 在伺服器端快取一小時、瀏覽器
localStorage 再快取六小時，closure 的開始與結束最多會慢六小時才反映。
`gym_live` 走五分鐘路徑，符合需求 R4。payload 增加約 150 bytes，對 5 GB/月
的 egress 預算沒有影響。

一個欄位餵兩個用途：banner 讀它，panel 4 的 card 也讀它決定要不要顯示休館
狀態。第四個星期一當天 17:00 前，那條生成的區間同樣會出現在
`activeClosures` 裡，card 因此自動標休館——這是共用同一份真相的免費結果。

### 頁面

[`tools/public_template.html`](../../../tools/public_template.html)：

- 面板上方加一個 banner 元素，由 `activeClosures` 驅動。陣列為空時整個元素不
  進 DOM（需求 R3），不是留一個空白框。
- panel 4 的 card：該場館出現在 `activeClosures` 時，顯示休館狀態取代
  「count / delta vs typical」（需求 R2）。

### 為什麼 `grafana/weekly-dashboard.json` 躲不掉

live card 不是頁面自己畫的。`tools/render_public_html.py:extract_panel_js()`
把四個面板的 `getOption` JS **從 `grafana/weekly-dashboard.json` 抽出來**烘焙
進頁面——Grafana 與公開頁面共用同一份渲染程式碼。卡片上的每一個字都由
panel 4 的那段 JS 產生，所以「card 顯示休館」除了改那個檔案沒有別的地方可改。

改動刻意設計成**在 Grafana 端完全惰性**：closure 資訊透過
`makeContext()` 新增的 `context.closures` 傳入，那是公開頁面自己組的 context；
Grafana 給的 context 沒有這個欄位，panel JS 讀到 `undefined` 就退回成今天的
行為。所以 `weekly-dashboard.json` 的檔案內容會變，Grafana 的畫面不會。

沒有走「讓 `gym_panel4_rows()` 多回一個 `kind = 'closed'` 列」那條路，因為那
要改函式的 `RETURNS TABLE` 型別，`CREATE OR REPLACE` 做不到、得 DROP 再建，
而它同時是 Grafana 面板 `rawSql` 的來源。用 context 傳遞不動任何 SQL 介面。

[`tools/render_public_html.py`](../../../tools/render_public_html.py) 只需要在
把 `venueDisplay` 傳進頁面的既有機制上運作——banner 的文案由 `reason`
（資料庫裡存英文）加日期在瀏覽器組出來，不烘焙任何日期進頁面。頁面已是英文
介面，venue 名稱沿用既有的中文 key → 英文顯示名對照。

### 權限

`public.closures` 與兩個新 view 會被 Supabase 的 PostgREST 自動曝露，所以照
[`004_rls_policies.sql`](../../../sql/migrations/004_rls_policies.sql) 既有的
做法處理（需求 R7）：

- 004：兩個新 view 設 `security_invoker = true`；對 `anon` / `authenticated`
  `REVOKE ALL`；`closures` 開 RLS 並給 `gym_reader` 一條 SELECT policy。
- 003：`GRANT SELECT` 給 `gym_reader`——兩個 view 都是 invoker rights，
  Grafana 讀不到底層物件就會失敗。

**沒有 `active_closures` view。** 第一版設計有，但它只有 `gym_live` 一個呼叫
點，而在 `public` 多一個物件的代價是一次 REVOKE、一個 RLS 決策、一條 grant 和
一項驗收檢查。`gym_live` 直接對 `closure_periods` 加 `now()` 條件即可。

不是為了保密（公告本來就公開），是因為那會是一條繞過
`api_private.rate_limit_check()` 的無節流 egress 途徑。

`gym_heatmap` / `gym_live` 是 SECURITY DEFINER，以 owner 身分執行，owner 繞過
RLS，因此不需要為 `anon` 開任何 policy。

### 套用之後要手動戳一下快取

`api_private.gym_heatmap_cache` 存的是組好的 heatmap payload，由 `pg_cron`
每小時重建一次。migration 套用完之後，快取裡仍然是**過濾前**的那份，最多會
再服務一小時。

所以部署步驟的最後一步是 `SELECT api_private.refresh_gym_heatmap_cache();`。
這不只是為了快——A1 的驗收是打 API 看數字有沒有變，不重建的話會誤判成沒生效。

（`gym_live` 沒有這個問題，它每次都重算。）

---

## 測試

`test_grafana_weekly_views.py` 的 fixture 加 closure 列，斷言：

- closure 區間內的讀數被剔除、區間外保留
- 第四個星期一 **16:59 剔除、17:00 保留**（邊界正好卡在 17:00）
- 第四個星期一規則對**兩個 venue** 都生效
- `venue` 非 NULL 的 closure 只影響該場館
- `current_vs_history` 的 `latest` 側不受 closure 影響（即時值照實）

該測試是 destructive integration test，未武裝時 skip，所以
`python3 -m unittest discover -p 'test_*.py'` 仍然隨時可跑。

---

## Minimal scope

### Files

- `sql/grafana_weekly_views.sql` — closures 表 + seed + 兩個 view；兩個既有
  view 改讀 `occupancy_excluding_closures`
- `sql/migrations/003_roles_grants.sql` — `gym_reader` 的 SELECT
- `sql/migrations/004_rls_policies.sql` — invoker rights、REVOKE、RLS + policy
- `sql/migrations/005_public_api.sql` — `gym_panel1_meta_rows` 改讀新 view；
  `gym_live` 加 `activeClosures`
- `tools/public_template.html` — banner；`makeContext()` 多傳一個 `closures`
- `grafana/weekly-dashboard.json` — **只改 panel 4 的 `getOption` JS**，加上
  休館卡片的渲染。見下方「為什麼這個檔案躲不掉」
- `test_grafana_weekly_views.py` — closure fixture 與邊界斷言
- `CLAUDE.md` — 記錄 closures 這一層，以及「開館時間有五處」之外新增的一處
  休館定義
- `docs/plan/2026-08-09-github-actions-supabase-migration.md` — D1 標記為已完成

### Behaviors

- 一次性區間與每月循環兩種 closure 都被歷史統計排除
- 半開區間 `[starts_at, ends_at)`，9/20 含當天、9/21 零時恢復
- 月休只套用在健身中心與室內游泳池兩個具名場館，其他場館不受影響
- 整日區間與當日區間的日期字串格式不同（`17 Aug – 20 Sep` vs
  `24 Aug · until 17:00`），由 `closureSpan()` 一處決定，banner 與 card 共用
- 第四個星期一的 17:00 邊界（16:59 排除 / 17:00 保留）
- `activeClosures` 為空時 banner 不進 DOM
- 休館場館的 card 不顯示 delta vs typical
- 所有 migration 維持 idempotent，重跑安全
- 裸 `postgres:17`（無 `anon` / `gym_reader` 角色）套用不報錯

## Explicit non-goals

### Files / pages not touched

- `fetch_counts.py`、`sync_to_postgres.py` — 採集端照收（需求 R6）。沒有原始
  資料就沒得反悔。
- `grafana/weekly-dashboard.json` 的 **panel 2**（`OPEN_HOURS_MIN`）— 熱區圖
  的軸是「星期 × 時段」，本質上表達不了「八月十七日」或「某月的第四個週
  一」。Grafana 也不加 banner（本機自用，banner 是給公開訪客的）。panel 4 的
  JS 則必須改，理由見上。
- `.github/workflows/backup.yml` — 不擴充成 dump `closures`。理由見上：git
  是真相。
- `README_Server.md`、`README_Local.md`、ws7 的舊頁面 — D3 文件收尾，另案。
- `tools/render_public_html.py` — 預期不需要改。banner 文案在瀏覽器端由
  `activeClosures` 組出來，沒有東西要烘焙進頁面。若實作時發現需要（例如
  banner 需要一個可設定的字串），視為超出範圍，回頭確認。

### Edge cases not handled

- **panel 3 折線在休館期間不斷線** — 已知限制。休館期間那條 7–11 的平線會照
  畫，由 banner 解釋。斷線這件事 `docs/TODO.md` 已有獨立的 "honest data-gap
  handling" 項目在追，且該項目本身尚未決定要不要做；併進來等於順手決定一個
  未決的設計問題。
- **手動 `UPDATE` 的 closure 不會進備份** — 已知限制，理由見「Seed 用
  `DO UPDATE`」。正確做法是改 migration。
- **整修若延期，需要人工修改並重新套用 migration** — 已知限制。沒有任何自動
  偵測公告變更的機制，也不打算加：那是一個爬蟲子系統。
- **`closure_periods` 的第四個星期一只生成到「下個月」** — 刻意如此。
  `activeClosures` 只關心 now()，歷史過濾只關心已有資料的範圍，兩者都不需要
  更遠的未來。
- **closure 的「開始」最多慢一個輪詢週期才顯示** — 已知限制。`gym_live` 只回
  傳當下生效的區間，所以頁面看不到「即將開始」的休館，無法預排計時器；結束
  則有精確計時器（`to` 已在手上）。五分鐘的延遲對一則休館公告沒有影響。
- **8/18 起健身中心讀數是 7–11 而非 0** — 不處理。看起來像施工人員刷卡或計數
  器殘留，但無從查證；closure 過濾是按時間區間排除，不管數值是多少，所以這
  件事不影響正確性。
- **2026-06-22 那個第四個星期一無法驗證** — 落在 ws7 遺失的六週資料洞裡
  （2026-06-22 → 08-07）。規則的證據來自 2026-05-25 單一日期加上網站頁尾的
  明文，兩者一致。
- **其他未被公告的臨時休館（颱風、場地借用）** — 不在範圍。closures 表的存在
  讓它們日後變成一次 INSERT，這已經是這次改動能提供的全部。
