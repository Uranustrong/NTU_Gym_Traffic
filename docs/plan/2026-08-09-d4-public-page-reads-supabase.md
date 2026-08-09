# D4：公開頁改成前端直接查 Supabase

> **狀態（2026-08-09）：已實作並驗證完畢。** 下面保留的是核准當下的設計，不隨實作更新；實際部署狀態看 `CLAUDE.md`。
>
> 這份計畫被退回兩次才通過。第一版有五個設計缺口，第二版還剩兩個 P0——`gym_reader` 會失去函式執行權讓 Grafana 整片壞掉，以及把 panel 3 統一成 `p_days` 會廢掉 Grafana 的時間選擇器。兩個都是「看起來合理、跑起來才炸」的那種，記在這裡是因為它們正是這份計畫最後長成現在這樣的原因。
>
> ## 實測數字
>
> - **移植 parity**：新 RPC 對舊 Python 路徑，逐 panel 逐欄位比對 parse 後的語意值——10 組、2,616 列、**零差異**。這一步在刪掉 Python SQL 之前做。
> - **權限邊界**（查 `has_function_privilege`，不靠 HTTP 推斷）：`anon` 只有 `public` 那兩支 wrapper；`authenticated` 全部為 `f`；`gym_reader` 十四支全通；`gym_writer` 全無。`anon` 連 `api_private` 的 schema `USAGE` 都沒有。
> - **真 key 打 HTTP**：兩支 wrapper `200`；`occupancy` / `weekly_occupancy_slots` / `current_vs_history` 仍 `401`；`rpc/gym_panel3_rows` `404`。
> - **`p_days` 夾制**：`9999` 與 `31` 的完整 panel3 值相同；`-5` 夾成 1 天；`null` 走預設 7 天。
> - **Egress**：heatmap 47 KB → 5.2 KB gzip（6 小時 TTL）、live 90 KB → 7.6 KB gzip（每 5 分鐘）。每個常開分頁約 66 MB/月，Free plan 5 GB 上限約可容納 75 個常開分頁。
> - **頁面**：載入時剛好兩次 RPC；之後切換 venue / granularity / palette **零網路請求**。頁面本身從 124 KB 降到 36 KB。
> - Grafana 少了 3,842 字元的重複 SQL，四個 panel 以 `gym_reader` 實際跑通。
>
> ## 只有實測才抓得到的兩個 bug
>
> 都在同一個地方，也都是「頁面會安靜說謊」那一類——正是這個狀態列存在的理由：
>
> 1. `noteFreshness` 原本只接受比現有更新的 `dataAsOf`（用意是調和同輪 heatmap 與 live 的時間差），結果**快取裡較新的值會蓋掉這次真正取得的較舊值**，collector 停掉時頁面永遠不會警告。
> 2. `gym_heatmap` 的 `dataAsOf` 也被算進來，但它有 6 小時 TTL，於是**幾小時前的時間戳會冒充最新讀數**。
>
> 修法比原設計更簡單：**新鮮度只由 `gym_live` 決定，且每輪重算**，不累積、不取 max。
>
> ## 與計畫的偏離
>
> - **`Authorization: Bearer` 那個 P0 沒有重現。** key 確實不是 JWT（`sb_publishable_` 前綴），但實測加上該 header 一樣回 200——Supabase 的 gateway 認得前綴、沒拿去解析成 JWT。`apikey`-only 仍然保留，因為那是官方文件的形式、同一份憑證塞兩個 header 沒有好處、且 gateway 行為可能隨版本改變；但它不是在修一個當下會壞的東西。
> - **Grafana 的 `rawSql` 多加了顯式 `ORDER BY`**（計畫沒寫）。SQL 函式被 inline 進外層查詢時，函式內部的 `ORDER BY` 不保證會被保留；JSON wrapper 那側已用 `row_number()` 重新固定，Grafana 這側不該賭。
> - **開館時間仍是五處，沒有變六處。** `api_private.gym_is_open()` 同時服務 panel 3 的過濾與 `freshnessExpected`，把 `render_public_html.py` 原本那份併掉了。
> - `cron.unschedule` 的迴圈變數不能叫 `job`——會和 `cron.job` 表名衝突，PL/pgSQL 判定 ambiguous。
> - 前端驗證用本機 stub server 模擬 PostgREST 完成，四種狀態各測一次；真 key 只用在最後的 HTTP 邊界驗證與線上頁面。
>
> ## 安全審查後補的防護
>
> 計畫只把「要拿 publishable key、不要拿 secret key」寫在文件裡，那是不夠的——貼錯一次就是把繞過所有 RLS 的金鑰印上公開網頁，而且**只能輪替、無法收回**。原本的 workflow 只檢查長度 ≥ 20，但 `sb_secret_...` 輕鬆超過。
>
> - `tools/render_public_html.py:check_publishable_key()` 驗證金鑰**宣稱自己是什麼**：接受 `sb_publishable_...` 與 `role == "anon"` 的 legacy JWT，拒絕 `sb_secret_...` 與任何其他 role，並在寫出任何檔案之前就退出（exit 2）。錯誤訊息永不回聲金鑰本身——public repo 的 CI log 是全世界可讀的。
> - `publish.yml` 改成對**實際要部署的檔案**斷言：`sb_secret_` / `service_role` 不得出現、設定的金鑰必須出現。這樣即使 renderer 日後被改壞，洞也不會靜默地重新打開。
> - 變數更名為 `SUPABASE_PUBLISHABLE_KEY`（`SUPABASE_ANON_KEY` 仍作為 fallback 讀取）。
> - 新增 `test_render_public_html.py`，10 個測試，其中一個明確斷言「只靠長度檢查抓不到這些」。
>
> ## 出貨前補的成本控制（2026-08-09 稍晚）
>
> 上面那條「每個常開分頁約 66 MB/月、5 GB 約可容納 75 個」只算了**善意流量**，那是這份計畫真正漏掉的一格。惡意流量的數字完全是另一個量級：單執行緒的 `curl` 迴圈打 `gym_live` 就能穩定跑出 1.9 GB/天，**2.6 天燒完整月 egress**；而 CPU 更早死——`gym_heatmap` 每 700 ms 的來回要吃 510 ms 的工，一支迴圈就占掉共用 CPU 的 ~73%，採集的 `COPY`（175 ms）得跟它搶。egress 爆掉至少會寄信給你，CPU 被餓死只會安靜地掉讀數，而掉掉的五分鐘是永遠回不來的。
>
> - **邊緣快取這條路實測不通。** 發一支 probe 函式送出 `Cache-Control: public, max-age=3600`，header 確實抵達瀏覽器，但 Supabase 的 Cloudflare 對 `/rest/v1/rpc/*` 一律回 `cf-cache-status: DYNAMIC`。所以全部得在資料庫裡解決。
> - **`gym_heatmap` 改成排程算、不是每次算：510 ms → 2.0 ms**（穿過 PostgREST 量的，255 倍）。它的成本全在 `weekly_occupancy_slots` 的全歷史聚合，而那東西一天才有意義地變一次。`pg_cron` 每小時重建，wrapper 只讀一列。**空的 payload 也算過期**——剛 initdb 的資料庫會在還沒有任何讀數時就把空清單存進去，restore drill 與全新的 docker stack 都會踩到。
> - **三層限流，中間那層是為台大加的。** 校園 wifi 是 NAT，所以每分鐘的額度不能照「一個人」抓——60/分鐘約等於一個 IP 後面 300 個分頁。中間補一層「每 IP 每日」，否則寬鬆的每分鐘額度會讓單一 IP 在午餐前吃光全域預算。
> - **`PT429` → HTTP 429 是實測的，不是假設的。** 而且 `RAISE` 會把計數的 increment 一起 rollback，所以被擋的呼叫端會把計數器釘在上限而不是無限往上跑，也不消耗全域預算。
> - **只有 HTTP 呼叫者受限**：client key 取自 `request.headers` 的 `cf-connecting-ip`，直連 libpq 沒有這個東西，所以 migration、`psql`、`pg_cron`、Grafana 全部自動豁免，不需要白名單。
> - 兩支 wrapper 因此必須從 `STABLE` 改成 `VOLATILE`（`STABLE` 不能寫入，而計數就是寫入）。代價可控：頁面本來就用 POST，只有 PostgREST 的 GET 形式要求 `STABLE`。
> - **前端一行都沒改。** 429 對頁面而言就是一次失敗的 RPC，既有的 5s→300s backoff 與快取回退路徑原封不動接住。
> - 順帶查到 `anon` 對 `net.http_post` 與 `cron.schedule` 有 EXECUTE。**那是 Supabase 出廠預設，不是我們給的，而且搆不到**：PostgREST 只曝露 `public`（`rpc/http_post` 是 404），`anon` 又是 `NOLOGIN`。不要去「修」它。
>
> 刻意**沒**做的是 `gym_live` 的 delta 協定（`p_since`）。它能把穩態輪詢砍 25 倍，但它優化的是離上限還有兩個數量級的善意流量，對濫用者毫無用處（不帶 `p_since` 就好），還會在剛驗證完的頁面裡引入一整類「快取有洞」的 bug。記進 `docs/TODO.md`，附上該在什麼訊號下重啟。
>
> ## 順帶確認
>
> `tools/recover_from_public_page.py` 有 `from render_public_html import VENUE_DISPLAY`——`VENUE_DISPLAY` 保留了，所以沒斷。但那個工具本身的用途已經隨這次改動失效：公開頁不再內嵌資料，未來的歸檔副本沒有東西可以救。

## Context

公開頁目前由 `tools/render_public_html.py` 把四個 panel 的資料**內嵌**進 `index.html`，所以頁面要新就得重新部署。今天已經把 publish 從 15 分鐘拉到 5 分鐘（pg_cron 觸發），代價是 192 次/天的 workflow run 只為了搬同一份 JSON。

改成前端直接呼叫 Supabase 之後：HTML/JS 變成純靜態、部署一次就好，資料由瀏覽器自己拉。publish 從「每 5 分鐘的資料管線」降級成「改程式才跑的部署動作」。

代價是 `anon` 目前在三個物件上權限全空（migration 004），要開一個口。下面把口開到最小：**anon 只拿到兩個唯讀 wrapper 的 EXECUTE，其餘一切放在 PostgREST 看不到的 schema，004 一行不改。**

已驗證：Supabase REST 回 `access-control-allow-origin: *`，Pages 的 origin 打得到。

### 前兩版計畫踩到的錯，這版都已修正

- 新版 publishable key 不是 JWT，不能放進 `Authorization: Bearer`
- 「採集停止 → 專案暫停 → 頁面顯示連不上」是錯的：訪客自己的輪詢就會讓 Free 專案保持活躍，collector 死掉時 RPC 照樣回 200
- `REVOKE ... FROM PUBLIC` 不等於「只有 anon 能執行」
- **`gym_reader` 會失去函式執行權**，Grafana 指向 Supabase 時整片壞掉
- **Grafana panel 3 用的是 `$__timeFilter` 與 TIMESTAMPTZ，公開頁用的是 `now() - N days` 與 epoch ms**，強行統一成 `p_days` 會廢掉 Grafana 的時間選擇器

## 需要你先提供

**Supabase 的 anon / publishable key**（Dashboard → Project Settings → API）。新專案顯示為 `sb_publishable_...`，舊的是 anon JWT，兩者都可以。這把 key 設計上就是公開的（會出現在頁面原始碼），所以存成 GitHub **variable** 而非 secret：

```
gh variable set SUPABASE_URL      --body 'https://gemuqplvzluuccuzmtwn.supabase.co'
gh variable set SUPABASE_ANON_KEY --body '<key>'
```

同名兩個變數也加進 `supabase.secrets`，並更新 `supabase.secrets.example`。

---

## 1. SQL：`sql/migrations/005_public_api.sql`

### 1a. canonical relational primitives（放在 `api_private`）

只有兩支 wrapper 需要被 PostgREST 看見，其餘放進 PostgREST 不曝露的 schema。這讓 anon 對它們是**看不到（404）**而不是「看得到但被拒絕（403）」，也降低日後誤 grant 的風險。

```
api_private.gym_panel1_typical_rows(p_venue text)
api_private.gym_panel1_now_rows(p_venue text)
api_private.gym_panel1_meta_rows(p_venue text)
api_private.gym_panel2_rows(p_venue text, p_gran text)
api_private.gym_panel3_rows(p_venue text, p_from timestamptz, p_to timestamptz)
api_private.gym_panel4_rows()
```

**panel1 拆成三支**，不是一支帶 UNION 的函式。外層過濾 `kind='now'` 無法保證 PostgreSQL 會裁掉 typical/meta 的 CTE，拆開才有結構性保證：heatmap wrapper 呼叫 typical + meta，live wrapper 只呼叫 now。

**panel3 收時間範圍而非天數**，回傳 `TABLE("time" timestamptz, metric text, value integer)`。這是兩條路唯一語意不同的地方（見上方對照），canonical 層必須用較general 的那個；`gym_live` 自己算 `p_from = now() - p_days * interval '1 day'`、`p_to = now()`，epoch ms 的轉換留在 JSON wrapper。

內容是 `tools/render_public_html.py:120-235` 的忠實移植。`p_days` 夾制放在 wrapper：`least(greatest(coalesce(p_days,7),1),31)`。

### 1b. 給瀏覽器的 JSON wrapper（放在 `public`）

兩支 `STABLE SECURITY DEFINER`，組出 `{"fields":[{"name":…,"type":…,"values":[…]}]}`，也就是 `to_series()` 現在產生的形狀，前端因此不需要移植 `_coerce()` 與 `PANELn_FIELDS`：

| 函式 | 內容 | 前端頻率 |
|---|---|---|
| `public.gym_heatmap()` | venue 清單、panel2（所有 venue × `30min`/`60min`）、**panel1 的 typical 與 meta** | 冷啟動一次，快取 TTL 6 小時 |
| `public.gym_live(p_days int default 7)` | panel1 的 now marker、panel3、panel4 | 載入 + 每 5 分鐘 |

panel1 的 typical bars 走 heatmap 而不是 live，是因為它就是 `weekly_occupancy_slots` 依今天 ISODOW 過濾的每小時 rollup，與 heatmap 的 `60min` series 同源。這把一整個歷史聚合掃描移出 5 分鐘的熱路徑。

兩支都必須回傳新鮮度三欄：

```json
{
  "dataAsOf": "<max(occupancy.fetched_at)>",
  "servedAt": "<now()>",
  "freshnessExpected": true
}
```

**`freshnessExpected` 由資料庫依 `Asia/Taipei` 判斷現在是否為開館時段。** 讓 JS 自己判斷會把開館時間定義從五處變成六處，而且會依賴訪客瀏覽器的時區。

### 1c. 資料契約

- 每個 `jsonb_agg` 都用 aggregate 內部的 `ORDER BY`，不依賴 CTE 輸出順序
- 空集合用 `coalesce(jsonb_agg(...), '[]'::jsonb)`，維持現行 Python `values: []` 的契約
- venue 中英對照**不進資料庫**：函式回傳真實中文名，`VENUE_DISPLAY` 留在 `render_public_html.py` 烘焙成頁面設定

### 1d. 權限

- 所有函式 `SET search_path = ''`，relation 一律完整限定（`public.occupancy`、`public.weekly_occupancy_slots`）
- `REVOKE EXECUTE ... FROM PUBLIC`，再顯式 `REVOKE EXECUTE ... FROM anon, authenticated`（存在才做，照 004 的 `pg_roles` 包法）
- `GRANT EXECUTE` 給 `anon` —— **只給 `public` 的兩支 wrapper**
- `GRANT USAGE ON SCHEMA api_private TO gym_reader` 與 `GRANT EXECUTE` 給 `gym_reader` —— **六支 relational functions 都要**。Grafana 的 Supabase datasource 用的就是這個角色（`grafana/provisioning/datasources/datasources.yml:14`），而 003 只給了 table/view 的 SELECT，沒有函式權限
- `CREATE OR REPLACE`、`CREATE SCHEMA IF NOT EXISTS`，維持冪等
- 所有 GRANT/REVOKE 都要能在沒有 `anon`/`gym_reader` 的乾淨容器上靜默跳過——`apply_migrations.sh` 會在 backup workflow 的 restore drill 裡對 `postgres:17` 套用這支

### 1e. 效能

- 對兩支 wrapper 跑 `EXPLAIN (ANALYZE, BUFFERS)`
- `current_vs_history` 會掃歷史算 percentile（`sql/grafana_weekly_views.sql:77`），panel4 又用 `DISTINCT ON (venue) ... ORDER BY venue, fetched_at DESC`；量測後若有需要，在 005 加 `(venue, fetched_at DESC)` index
- 量實際 gzip 後的 response 大小，估 `bytes × 同時開啟的 tab × 288/day` 對 Free plan 的 5 GB uncached egress

---

## 2. Grafana dashboard

四個 panel 的 `rawSql` 改成呼叫 canonical 函式，例如：

```sql
-- refresh on: ${palette} ${metric}
SELECT * FROM api_private.gym_panel2_rows(${venue:sqlstring}, '${granularity}');
```

```sql
-- refresh on: ${palette}
SELECT * FROM api_private.gym_panel3_rows(${venue:sqlstring}, $__timeFrom(), $__timeTo());
```

panel 1 的 facade 是三支 primitive 的 `UNION ALL`，維持現行的 `kind`/`hour`/`count`/`samples` 欄位與排序。

**開頭的 `-- refresh on: ${palette}` / `${metric}` 註解必須保留。** 它們的唯一用途是讓 Grafana 在那些變數改變時重跑查詢；刪掉的話切換 palette 或 metric 不會重繪。改的是 `rawSql`，不碰 `options.getOption`，所以 `extract_panel_js()` 不受影響。

連帶：`docker-compose.yml` 目前只把 `001`、`002` 掛進 `docker-entrypoint-initdb.d`，全新的本機 stack 不會有這些函式、Grafana 會壞掉，**005 必須一併掛進去**。而且 **initdb hook 只在空 data dir 的首次啟動生效**，既有的 volume 不會重跑——現有 stack 要另外對容器跑一次 `tools/apply_migrations.sh`。

---

## 3. `tools/public_template.html`

### 3a. 請求

**只送 `apikey`，不送 `Authorization`**：

```js
headers: { apikey: CONFIG.supabaseKey, "Content-Type": "application/json" }
```

publishable key 不是 JWT，放進 `Authorization: Bearer` 會被當成壞掉的 JWT；沒有登入使用者時本來就不該送。

### 3b. 四種狀態，依 `dataAsOf` 判斷而非「RPC 成功」

| 狀態 | 條件 | 顯示 |
|---|---|---|
| 正常 | RPC 成功，`servedAt - dataAsOf` 在門檻內 | `Updated HH:MM` |
| **採集落後** | RPC 成功、`freshnessExpected` 為真，但差距 > 15 分鐘 | `最後讀數為 X 分鐘前 · 採集可能已停止` |
| 資料源不可達 | RPC 失敗，有快取 | `使用快取 · 資料為 X 分鐘前 · 目前連不上資料源` |
| 首次載入失敗 | RPC 失敗且無快取 | 明確的載入失敗訊息，不畫空座標軸 |

第二列是關鍵：collector 掛掉時 RPC 仍回 200，若只看「RPC 有沒有成功」，頁面會不斷把舊資料重新存成看似剛更新的快取——failure mode 會從「明顯壞掉」變成「安靜說謊」。

### 3c. 快取

- **heatmap 與 live 分開存**：`gymdash:v1:<ref>:heatmap` 與 `gymdash:v1:<ref>:live:<days>`
- 各存 `{schemaVersion, dataAsOf, savedAt, payload}`，讀取時驗證 shape，不符即丟棄
- heatmap 設 6 小時 TTL，否則長時間不重載的分頁永遠拿不到新的 heatmap

### 3d. 輪詢

- 移除 `<meta http-equiv="refresh">`，改成原地更新（捲動位置與下拉選單選擇保留）
- `document.visibilityState === "hidden"` 時停止輪詢，回到 visible 立即刷新
- 單一 in-flight guard，禁止重疊請求
- 失敗採 exponential backoff（5s 起，上限 5 分鐘）

### 3e. 命名與翻譯

- `CONFIG`（建置期烘焙：`supabaseUrl`、`supabaseKey`、`venueDisplay`、`panelJs`、`days`、`refreshSeconds`）與 runtime `DATA`（RPC 取得）明確分開，不再出現 `DATA.panelJs`
- JS 內部一律以**真實中文 venue 當穩定 key**；selector 的 `value` 用中文、`textContent` 用英文
- panel1 標題、panel3 的 `metric`、panel4 的 `venue` 都要翻譯，不是只翻下拉選單

四個 `renderPanel` 與 `makeContext()` 的核心邏輯可以沿用，但 **selector 初始化與首次 `renderAll()` 必須改寫**——venue 清單現在是非同步取得的，不再是載入時就存在的常數。

---

## 4. `tools/render_public_html.py`：不再碰資料庫

刪除 `sql_panel1/2/3`、`SQL_PANEL4`、`SQL_VENUES`、`run_query`、`load_env`、`build_postgres_url`、`to_series`、`_coerce`、`translate_field`、`PANELn_FIELDS`。保留 `extract_panel_js()`、`VENUE_DISPLAY`、echarts 複製、模板替換。

參數：`--supabase-url`、`--supabase-key`、`--days`、`--refresh-seconds`；移除 `--postgres-url`、`--psql`。**前兩者 default 必須明確讀 `os.environ.get("SUPABASE_URL")` / `("SUPABASE_ANON_KEY")`**，否則「加進 `supabase.secrets` 再 source」不會生效。缺值 fail fast。

結果：**publish 不再需要任何資料庫憑證。**

## 5. `.github/workflows/publish.yml`

- 觸發改成 `push`（**`branches: [main]`** + paths：`tools/**`、`grafana/weekly-dashboard.json`、`.github/workflows/publish.yml`）+ `workflow_dispatch`，移除 `schedule`。只寫 `paths` 會讓 feature branch 的 push 也部署到正式 Pages
- 移除 `PG_URL`/`PGPASSWORD`/`PGPORT`/`PGSSLMODE`，改用 `vars.SUPABASE_URL`、`vars.SUPABASE_ANON_KEY`
- 健全性檢查換條件（頁面已無 venue 名稱與資料）：沒有未替換的 `{{...}}`、host 等於預期 project host、key 非空（**只檢查長度，不印內容**）、`echarts.min.js` 非空

## 6. 撤掉 publish 的 pg_cron job

`sql/supabase/001_dispatch_cron.sql` 把四個 `publish-*` 改成 `cron.unschedule`（存在才做），並註明原因。collect 的四個 job 不動。

## 7. 文件

`CLAUDE.md` 要改的不只「歸檔救援消失」：workflow 表格（publish 的 cadence / woken by / role / port 全變）、GitHub variables 清單、pg_cron 從 8 個變 4 個、migration 清單多 005、`api_private` schema 的存在與理由、「public static page」的描述。

`docs/TODO.md` **沒有可刪的 D4 項目**；要改的是 week-navigation 那一項裡「公開頁是 static、所有資料都得烘焙」的限制說明——那個限制在這次改動後失效，但功能本身仍然有效，不要整項移除。

---

## 要一併說清楚的事

**歸檔救援這條路會消失。** `docs/recovery/` 那 2,444 列救得回來，正是因為舊頁把原始讀數烘焙進 HTML。改完之後公開頁只剩空殼。替代品是每週 `backup.yml` 的 dump + restore 驗證 + `data-latest` release——刻意設計的備份優於意外的副產品，但這個轉換要記進 `CLAUDE.md`。

**負載模型從固定變成隨訪客而變。** 原本固定 192 runs/day，改完之後每個持續開啟的分頁最多 288 次 `gym_live`/天，而且 RPC 公開、任何人都能反覆呼叫。`p_days` 的 clamp 只限制結果大小，不限制呼叫頻率。1e 的量測與 3d 的 visibility 控制就是為此。

---

## 驗證

1. **移植正確性（最重要，且必須在刪掉 Python SQL 之前做）**：同一時刻取得新 RPC 與現行 Python 路徑的輸出，**比較 parse 後的語意值**逐 panel 逐欄位比對——不要對原始 JSON bytes 取 digest，jsonb 的 key 順序與 `1`/`1.0` 表示法可能不同。四個 panel 全相符才算移植成功。
2. **Grafana parity**：對本機 docker stack（已套 005）四個 panel 正常顯示；**panel 3 的時間選擇器（前一週、自訂範圍）仍然有效**；切換 palette / metric / granularity 會重繪。
3. **權限邊界**：
   - anon key 打兩支 wrapper → 200；打 `/rest/v1/occupancy`、`weekly_occupancy_slots`、`current_vs_history` → 仍 401/403；打 `rpc/gym_panel3_rows` → 404（不在曝露的 schema）
   - 直接查 `proacl` / `has_function_privilege()` 確認 `anon` **與 `authenticated`** 的實際授權，不要只靠 HTTP 推斷
   - `SET LOCAL ROLE gym_reader; SELECT * FROM api_private.gym_panel1_typical_rows('健身中心');` 必須成功
4. **`p_days` 夾制**：`gym_live(9999)` 與 `gym_live(31)` 比對**完整 panel3 值**，不只列數。
5. **restore drill 不受影響**：`tools/apply_migrations.sh --skip-roles` 對乾淨的 `postgres:17` 容器套用（005 的 GRANT/REVOKE 因為角色不存在而靜默跳過），`python3 -m unittest discover -p 'test_*.py'` 全綠。
6. **效能**：兩支 wrapper 的 `EXPLAIN (ANALYZE, BUFFERS)`；記錄 gzip 後 response 大小並估算月 egress。
7. **頁面**：四個 panel 有資料；venue / palette / granularity 切換即時；等 5 分鐘確認原地更新且捲動位置不變；切到別的分頁輪詢停止、切回立即刷新。DevTools 的期待值分冷熱：**冷啟動 `gym_heatmap` 1 次、`gym_live` 1 次；6 小時內重載 `gym_heatmap` 0 次**。
8. **四種狀態各測一次**：正常；把 `supabaseUrl` 指到壞位址 → 快取加「連不上」；清掉 localStorage 再試 → 載入失敗訊息；手動讓 `dataAsOf` 變老 → 「採集可能已停止」。
9. **端到端**：改一行模板 push 到 main → 只有 publish 被觸發、collect 不受影響 → 頁面幾分鐘內帶上新內容；再從 feature branch push 一次，確認**不會**部署。
