# 把 NTU_Gym_Traffic 的收集層搬到 GitHub Actions + Supabase

## Context

收集器原本跑在 `ws7.csie.ntu.edu.tw:/tmp2/b12902066/Gym_Fetch`，`/tmp2` 是機器本地的 scratch 空間，已於 **2026-08-08 前後被清空**（`crontab -l` 回 `no crontab`，`/var/spool/cron/crontabs/` 不存在，Gym_Fetch 目錄無殘留）。

**實際損失比原先以為的大。** 採集其實一路運作到 2026-08-07 21:55；本機 `gym_counts.sqlite3` 停在 2026-06-21 只是因為那天之後沒再跑 `rsync.sh pull-data`。從公開頁殘存的 `panel2` 聚合反推（`sum(sample_count)` = 14,900/venue）：

| | 每 venue | 總計 |
|---|---|---|
| 清空前實際存在 | 14,900 | 29,800 |
| 本機 SQLite 現有 | 6,510 | 13,020 |
| 可從公開頁救回（見 A0） | 1,222 | 2,444 |
| **永久遺失（2026-06-22 → 07-31）** | **7,168** | **14,336** |

single point of failure 不是理論風險，已經實際造成約 48% 的歷史資料損失。這也是 Phase A 把備份列為 blocker 的直接理由。

目標：scheduler + 儲存都移出 ws7。GitHub Actions 定時抓取，Supabase PostgreSQL 成為唯一線上資料源，公開頁改由 GitHub Pages 發佈，ws7 全面退場。

計畫分兩段：**ws7 退場前先把 Phase A 的 blocker 修完**。遷移完成的那一刻 Supabase 就從「第二份拷貝」變成「唯一拷貝」，現在還能容忍的弱點（runtime 持有過大權限、staging table 會被互洗、沒有備份）到時候就沒有安全網。

### 兩項已用實際資料驗證的事實

1. **平日開館維持 `06:00`。** 健身中心台北時間 06:00 / 07:00 的每日 peak，2026-05-18 → 06-18 每個平日都非零（06:00 約 6–17 人，07:00 約 19–34 人）。commit `28a4ae6` 把 08→06 是刻意修正。官網 `8:00~22:00` 是**綜合體育館建物**的時間。改成 08:00 會每個平日丟掉兩小時真實資料。

2. **第四個星期一休館規則屬實。** 2026-05-25 健身中心 06:00–16:00 每小時 peak 為 4, 5, 6, 2, 2, 3, 2, 13, 13, 0, 0，17:00 跳到 121；對照正常週一 06-01 同時段 12, 21, 37, 46, 50, 57, 60, 54, 57, 62, 76。目前只有這一天受影響。**只在 view 層過濾，採集端照收**（D1）。

### 三個曾經考慮但否決的做法

1. **不要把 `fetched_at` 對齊到 5 分鐘網格。** 動機是「維持與歷史資料的對齊、讓 retry 冪等」，但 GitHub 的排程延遲經常超過 2.5 分鐘，對齊等於把 06:06 抓到的讀數寫成 06:05 —— 用「時間戳不整齊」這個無害問題換到「時間戳說謊」這個有害問題，而且從此量不到 Actions 的真實延遲。記錄真實時間，統計交給 30 分鐘桶處理。

2. **不要把資料 commit 進 repo。** 曾考慮每週把 Supabase 匯出成每月 CSV 存進 `data/`。git 每個版本都永久保留，壓縮過的 CSV 無法行級差分，repo 只會單調變大，事後想縮小得改寫 history；而且**每次 `git clone` 都要下載全部歷史資料**，即使只想看程式碼。改用兩個「檔案掛在 GitHub 上但不進 git object database」的機制：
   - **Actions artifact**（附在單次 workflow run，`retention-days: 90` 到期自動清）→ 放 `pg_dump` 的 `.dump`
   - **Release asset**（附在 tag，永久保存、有穩定下載網址）→ 放公開的 `occupancy.csv.gz`

   repo 只留 code、migration、workflow、測試。注意 public repo 的 artifact 與 Release asset 任何人都能下載 —— 這也是 A4 只 dump `public.occupancy` 的理由。

3. **不要用 `crontab -r` 退場。** 那是「刪除整份 crontab」，多數系統不確認、沒有 undo。ws7 是共用工作站，你的 crontab 裡可能還有與本專案無關的 job。改成 B6 的備份 + 過濾 + 肉眼確認三步。

---

# Phase A — Cutover 前必須完成

## A0. 先搶救公開頁裡殘存的一週資料（最優先，與其他工作無依賴）

`https://www.csie.ntu.edu.tw/~b12902066/gym/` 仍在服務 **2026-08-08 03:29 CST** 產生的靜態頁。它不在 `/tmp2`（所以逃過清空），而 `render_public_html.py` 把資料 inline 進 HTML —— 其中 `panel3` 是 `--days 7` 的**原始讀數**，欄位正好是 `(fetched_at, venue, current_count)`（[tools/render_public_html.py:171-189](tools/render_public_html.py#L171-L189)）。

已驗證可完整重建 **2026-08-01 09:00 → 2026-08-07 21:55 共 2,444 列**，每日筆數與應有節奏完全吻合（平日 192/venue、週六 156、週日 108；僅 08-05 少 2 筆）。`comfortable_count` / `capacity_count` 從 `panel4` 的 summary 列取得（健身中心 80/161、室內游泳池 50/130）。

檔案實際位置已確認為 `~/htdocs/gym/index.html`（166,203 bytes，mtime `2026-08-08 03:29`，與頁面上的 `Updated` 戳記一致 —— render 就是在那一刻停止的）。它在 NFS home 上，不在 scratch，所以沒有立即消失的風險，但仍應先落地。

實作步驟：
1. `scp b12902066@ws7.csie.ntu.edu.tw:htdocs/gym/index.html docs/recovery/gym-page-2026-08-08.html` —— **先把原始 HTML 存進 repo**。直接從 home 取比 `curl` 乾淨（不經 web 層轉換）。
2. 寫 `tools/recover_from_public_page.py`：brace-match 抽出 `const DATA = {...};`、把 `panel3` 的 columnar frame 展開成列、用 `{"Fitness Center": "健身中心", "Indoor Pool": "室內游泳池"}` 反查回中文 venue、`panel4` 的 summary 補 comfortable/capacity、輸出 CSV。
3. 併入 `gym_counts.sqlite3`，或在 B2 backfill 時一起送進 Supabase。

**重建列的 `source_updated_at` 為 NULL**（頁面沒有保留這個欄位）。這剛好是天然標記 —— 既有 13,020 列的 `source_updated_at` 全部非空，所以 `WHERE source_updated_at IS NULL` 可以精確識別出重建資料，不需要為此加欄位。這件事要寫進 `docs/recovery/README.md`。

`panel2` 的全歷史聚合（weekday × 30 分鐘 slot 的 avg / sample_count）也一併保存 —— 它涵蓋了永久遺失的那段期間，雖然無法還原成原始列，但保留了那段時間的分布形狀，日後若要說明資料缺口有用。

## A1. 重構 `sync_to_postgres.py`：單一 transaction + TEMP staging + 交易內驗證

**現況缺陷**（[sync_to_postgres.py:118-123](sync_to_postgres.py#L118-L123)）：三個獨立 psql process、三條連線。兩個 run 交錯時：

```
P1: SCHEMA_SQL → TRUNCATE occupancy_import
P1: COPY_SQL   → COPY 2 rows
P2: SCHEMA_SQL → TRUNCATE occupancy_import      ← 抹掉 P1 的資料
P1: UPSERT_SQL → INSERT ... SELECT → 0 rows
P1: print("synced 2 rows"), exit 0              ← 說謊，job 綠燈
```

workflow 的 `concurrency` 保護不到本機 backfill、手動重跑、或未來的其他 workflow。這是設計缺陷，不是排程問題。

**改法** —— `run_psql()`([:109-115](sync_to_postgres.py#L109-L115)) 與 `sync_rows()`([:118-123](sync_to_postgres.py#L118-L123)) 合併成一次 `psql -f -`，整份 script 從 stdin 餵入（`pg_dump` 的輸出格式，含 inline COPY data 與 `\.` 終止符）：

```sql
BEGIN;

CREATE TEMP TABLE occupancy_import (
    fetched_at TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMP,
    venue TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    comfortable_count INTEGER,
    capacity_count INTEGER
) ON COMMIT DROP;

COPY occupancy_import (fetched_at, source_updated_at, venue,
                       current_count, comfortable_count, capacity_count)
FROM STDIN WITH (FORMAT csv);
<csv rows>
\.

INSERT INTO occupancy (fetched_at, source_updated_at, venue,
                       current_count, comfortable_count, capacity_count)
SELECT fetched_at, source_updated_at, venue,
       current_count, comfortable_count, capacity_count
FROM occupancy_import
ON CONFLICT (fetched_at, venue) DO NOTHING;   -- backfill 模式改成 DO UPDATE，見下

-- 交易內逐筆驗證：每一筆 staged row 都必須以相符的值存在於 occupancy
DO $$
DECLARE missing int;
BEGIN
    SELECT count(*) INTO missing
    FROM occupancy_import i
    LEFT JOIN occupancy o
           ON o.fetched_at = i.fetched_at
          AND o.venue      = i.venue
          AND o.current_count IS NOT DISTINCT FROM i.current_count
          AND o.source_updated_at IS NOT DISTINCT FROM i.source_updated_at
    WHERE o.fetched_at IS NULL;
    IF missing > 0 THEN
        RAISE EXCEPTION 'sync verification failed: % staged row(s) missing or mismatched', missing;
    END IF;
END $$;

COMMIT;
```

這個設計同時解掉五件事：

- TEMP table 是 session-local → 併發互洗從根本消失。
- 整個操作原子化，驗證在同一交易內 → 不會出現「寫入失敗但回報成功」。
- **驗證的是本次這兩筆的精確 `(fetched_at, venue)` 與值**，不是「資料庫最近有資料」。
- **處理「已 commit 但回應前斷線」的不確定狀態**：retry 重跑整份 script，`DO NOTHING` 命中 conflict 插入 0 列，但驗證 JOIN 找到相符的列 → 成功。retry 因此是安全且冪等的。
- runtime 不再需要 `public` schema 的 DDL 權限（`CREATE TEMP TABLE` 仍是 DDL，但只需要 database 的 `TEMPORARY` 權限，不需要 schema `CREATE`）—— A2 的最小權限才可能成立。

**衝突策略要可切換**：runtime 用 `DO NOTHING`（配合 A2 只給 INSERT/SELECT）；backfill 與資料修補用 `--on-conflict update` 走 `DO UPDATE SET`，以 owner 憑證執行。

**同時修回報錯誤**：`sync_rows()` 目前回傳 `len(rows)`（[:123](sync_to_postgres.py#L123)），`main()` 印 `synced {count} rows`（[:143](sync_to_postgres.py#L143)）—— 那是**從 SQLite 讀出的列數**。改成解析 psql 的 `INSERT 0 N` tag。

**Schema 管理拆出去**：`--apply-schema` 或 `tools/apply_migrations.sh`，用 owner 連線執行 migration。日常 sync 不碰 DDL。

**測試更新**：[test_sync_to_postgres.py](test_sync_to_postgres.py) 目前斷言 `run.call_count == 3` → 改成 1；新增「psql 非零退出時不得回報成功」與「驗證失敗時整個交易 rollback」兩個 case。

## A2. 最小權限角色

以 owner（`postgres.<ref>`）執行。**寫成版本化 migration，不要只在 SQL Editor 手動跑**（見 A8）。

```sql
DROP TABLE IF EXISTS public.occupancy_import;   -- A1 之後全域 staging 已無用

CREATE ROLE gym_writer LOGIN PASSWORD '<writer-pw>';
CREATE ROLE gym_reader LOGIN PASSWORD '<reader-pw>';

GRANT USAGE ON SCHEMA public TO gym_writer, gym_reader;
GRANT TEMPORARY ON DATABASE postgres TO gym_writer;    -- TEMP staging 需要

-- writer：只能 SELECT + INSERT。沒有 UPDATE、DELETE、TRUNCATE、DDL。
-- SELECT 是 A1 交易內驗證所需；occupancy 的內容本來就會公開在 public page，不敏感。
GRANT SELECT, INSERT ON public.occupancy TO gym_writer;
GRANT USAGE, SELECT ON SEQUENCE public.occupancy_id_seq TO gym_writer;

GRANT SELECT ON public.occupancy,
                public.weekly_occupancy_slots,
                public.current_vs_history      TO gym_reader;
```

**為什麼不給 UPDATE**：整表 UPDATE 搭配 `USING (true)` 等於憑證外洩後可以把全部歷史人數改成零 —— 不能 DELETE 但能靜默破壞，實際危害相當。runtime 只需要新增，用 `DO NOTHING` 就夠。需要修補既有列時用 owner 憑證跑 `--on-conflict update`，那是人工、一次性、有意識的操作。

角色對應：`gym_writer` → collect；`gym_reader` → publish、backup、本機 Grafana；owner → 只用於 migration 與需要 `DO UPDATE` 的修補。三組憑證存成獨立 secret。

## A3. RLS + view 權限

Supabase 把 `public` schema 的 table 自動經 PostgREST 曝露，`anon` / `authenticated` 預設有權限（anon key 設計上就是公開的）。

而且**只替 table 開 RLS 會被 view 繞過**：[sql/grafana_weekly_views.sql](sql/grafana_weekly_views.sql) 的兩個 view 由 owner 建立，PG 預設 `security_invoker = off`，view 以 owner 身分執行，底層 RLS 不生效。

`occupancy_import` 已在 A2 移除，所以需要收斂的 PostgREST 物件是**三個**：`occupancy` 加兩個 view。

```sql
ALTER VIEW public.weekly_occupancy_slots SET (security_invoker = true);
ALTER VIEW public.current_vs_history     SET (security_invoker = true);

ALTER TABLE public.occupancy ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.occupancy,
              public.weekly_occupancy_slots,
              public.current_vs_history        FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.occupancy_id_seq FROM anon, authenticated;

-- RLS 開啟後非 owner 角色需要顯式 policy（owner 預設 bypass）
CREATE POLICY gym_writer_insert ON public.occupancy
  FOR INSERT TO gym_writer
  WITH CHECK (fetched_at >  now() - interval '1 hour'
          AND fetched_at <= now() + interval '5 minutes');
CREATE POLICY gym_writer_select ON public.occupancy
  FOR SELECT TO gym_writer USING (true);
CREATE POLICY gym_reader_ro ON public.occupancy
  FOR SELECT TO gym_reader USING (true);
```

`gym_writer_insert` 的時間窗把「即使憑證外洩也只能污染最近一小時」寫進資料庫本身。backfill 走 owner 憑證所以不受此限。

驗證（三個物件都應回 401/403）：
```bash
for t in occupancy weekly_occupancy_slots current_vs_history; do
  printf '%s ' "$t"
  curl -s -o /dev/null -w '%{http_code}\n' \
    "https://<ref>.supabase.co/rest/v1/$t?select=*&limit=1" -H "apikey: <ANON_KEY>"
done
```

## A4. 備份 + restore drill

Supabase Free plan **沒有** daily backup / PITR。ws7 時代至少有 SQLite 加 36 個 `.bak-*` 快照當天然備援；遷移後新資料只存在一處。

### 只備份需要的資料，且不用 owner

**不要對整個 database 做 owner dump。** 那會把 Supabase 的 Auth / Storage / 內部 schema 一起帶走（現在沒用到，將來可能會），而且違反 A2「Actions 不持有 owner 憑證」的目標。schema 與 migration 都已在 repo 裡，備份只需要資料：

```bash
pg_dump "$READER_URL" \
  --format=custom --data-only --enable-row-security \
  --table=public.occupancy \
  --no-owner --no-privileges \
  -f occupancy-$(date +%F).dump
```

**`--enable-row-security` 不是選配。** 實測時少了它，pg_dump 直接拒絕：`query would be affected by row-level security policy for table "occupancy"`。這是它的保護機制——不願默默產出可能不完整的備份。加上之後，dump 的內容等於 `gym_reader` 的 policy 看得到的範圍；目前 policy 是 `USING (true)` 所以是全部，但**這代表備份完整性從此依賴 policy**，所以下面的列數比對是必要步驟而非額外檢查。

**`gym_reader` 需要 sequence 的 SELECT。** `--data-only` 會讀 `occupancy_id_seq` 的 `last_value` 以便還原後接續編號，少了會失敗（已補進 `003_roles_grants.sql`；只給 SELECT 不給 USAGE）。

**失敗的 dump 會留下檔案。** 上述兩個錯誤分別留下 0 byte 與 1.5K 的殘檔——後者更危險，看起來像有效備份。**backup workflow 必須檢查檔案大小與還原後列數，不能只看 exit code。**

⚠️ **公開 repo 的 Actions artifact 任何有 read access 的人都能下載**，等同公開。因此 artifact 裡只能放已確認可公開的內容 —— `public.occupancy` 的資料本來就會呈現在公開頁面上，所以這個範圍是安全的；但這正是「不要整庫 dump」的理由。若未來 dump 內容包含非公開資料，必須加密或改放私人儲存。

**Release asset 只放 `occupancy.csv.gz`**（明確可公開的唯讀 snapshot），`.dump` 只放 artifact。

### pg_dump 版本必須動態對齊

`ubuntu-24.04` runner 內建 PostgreSQL **16** client。若 Supabase 跑 17，`pg_dump` 會直接拒絕。**不要寫死版本號** —— 先查伺服器再裝：

```bash
MAJOR=$(psql "$READER_URL" -X -A -t -c "SHOW server_version_num;" | cut -c1-2)
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  | sudo gpg --dearmor -o /usr/share/keyrings/pgdg.gpg
echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt-get update && sudo apt-get install -y "postgresql-client-${MAJOR}"
```

（collect / publish workflow **不需要**這段 —— `psql` 跨版本相容，只有 `pg_dump` 嚴格。）

### Restore drill —— cutover 前必做

沒演練過的備份等於沒有備份。data-only dump 必須**先套 migration 建結構，再 restore 資料**：

```bash
docker compose up -d postgres
docker exec -i gym-postgres psql -U songhejun -d postgres -c "CREATE DATABASE restore_drill;"
DRILL='postgresql://songhejun:...@127.0.0.1:5433/restore_drill'
./tools/apply_migrations.sh "$DRILL"          # 先建 schema / views
pg_restore -d "$DRILL" --data-only occupancy-$(date +%F).dump
psql "$DRILL" -c "SELECT count(*) FROM occupancy;"   # 應等於當時的實際列數
```

異地保存至少兩處（本機 + 雲端硬碟／隨身碟）。

## A5. `fetch_counts.py`

### A5.1 Timezone

GitHub runner 是 UTC。`datetime.now().astimezone()` 帶 host 時區 → `is_open_time()` 拿 UTC 的 hour 比對台北開館時間，workflow 有跑但判斷全錯且不報錯。

加 `from zoneinfo import ZoneInfo` / `TAIPEI = ZoneInfo("Asia/Taipei")` / `def now_taipei(): return datetime.now(TAIPEI)`，四處替換：

| 行 | 位置 | 重要性 |
|---|---|---|
| [:302](fetch_counts.py#L302) | `main()` 單次執行 | **關鍵路徑** —— 同時餵給 `is_open_time(now)`(303) 與 `fetched_at`(307) |
| [:261](fetch_counts.py#L261) | `run_loop()` | 本機 `--loop` 用 |
| [:37](fetch_counts.py#L37) | `DailyLogStream` log 檔名 | 非台北 host 會在早上 08:00 換檔 |
| [:184](fetch_counts.py#L184) | `save_rows()` default | 低風險（呼叫端都顯式傳入） |

`is_open_time()`([:137-145](fetch_counts.py#L137-L145)) 與 `opening_hour()`([:148-151](fetch_counts.py#L148-L151)) **不動** —— 傳入 aware-Taipei datetime 即正確，時數 6/9/22/22/18 是對的。workflow 另設 `env: TZ: Asia/Taipei` 當第二道保險。

**對既有資料零影響**：SQLite 存帶 `+08:00` 的 ISO 字串（13,020 筆全部如此，無 naive）；Postgres 是 `TIMESTAMPTZ`，比較 instant 不比較文字，offset 寫法不同不會產生假 duplicate 或漏掉 conflict；讀取端（`grafana_weekly_views.sql`、`render_public_html.py`）都已顯式 `AT TIME ZONE 'Asia/Taipei'`。純 correctness 修正，不需要 data migration。

### A5.2 擴大 exception 涵蓋（實質 bug）

[fetch_counts.py:234](fetch_counts.py#L234) 只 catch `URLError`。`urlopen(timeout=20)` 的**連線**逾時會包成 `URLError`，但**讀取**逾時拋裸的 `TimeoutError`，`http.client.RemoteDisconnected` / `IncompleteRead` 也不是 `URLError` —— 這些情況 retry loop 完全不生效，直接 traceback 死掉。校內網路罕見，但 runner 到 NTU 是跨太平洋公網。改成：

```python
except (OSError, http.client.HTTPException) as error:
```

（`URLError` 與 `TimeoutError` 都是 `OSError` 子類。）

### A5.3 資料完整性檢查

現況只要求「至少解析到一筆」([:244](fetch_counts.py#L244))，網站只回一個 venue 仍綠燈。新增：

- `--expect-venues 健身中心,室內游泳池` —— 集合必須**正好**相符
- `current_count >= 0`，且不得遠超 `capacity_count`
- **staleness 檢查有兩個坑**：
  1. `source_updated_at` 是**台北本地的 naive 字串**（`YYYY-MM-DD HH:MM`）。比較前必須顯式 `AT TIME ZONE 'Asia/Taipei'`（SQL 側）或 `.replace(tzinfo=TAIPEI)`（Python 側），否則會拿 naive 減 aware 直接 `TypeError`。
  2. **只在正常開館時段強制**。`force=true` 的煙霧測試在閉館時間跑，來源的 `source_updated_at` 自然會超過 15 分鐘，無條件強制會讓煙霧測試永遠失敗。另提供 `--allow-stale-source` 供 B1 使用。

### A5.4 真正的單元測試

`TZ=UTC python3 -c "... print(...)"` 只是印出結果，不是測試。新增 `test_fetch_counts.py`，固定 datetime 涵蓋邊界：

| 輸入（台北） | 期望 |
|---|---|
| 週一 05:59 / 06:00 / 21:59 / 22:00 | False / True / True / False |
| 週六 08:59 / 09:00 / 21:59 / 22:00 | False / True / True / False |
| 週日 08:59 / 09:00 / 17:59 / 18:00 | False / True / True / False |

外加 `TZ=UTC` 下建構台北時間再驗證，確保 A5.1 真的生效。

## A6. Pooler 選擇（依 A1 結果決定，B1 實測）

- **Migration、`pg_dump`、本機 Grafana** → Session pooler **:5432**。
- **collect / publish** → **:6543（transaction pooler）— 已實測確定**。用真實的 Supabase 專案（PG 17.6）跑 `sync_to_postgres.py`，每次都用全新的兩列，連續 5 次全數成功；`COPY ... FROM STDIN` + `CREATE TEMP TABLE ... ON COMMIT DROP` + 交易內驗證在 transaction mode 下完全正常，因為整個操作在單一 transaction 內、backend 全程被 pin 住。5432 也可用（單次延遲 1.09s vs 1.40s，n=1 屬雜訊），但短命 client 用 transaction mode 是 Supabase 的建議做法。**`PG_PORT` 設 6543。**

連線一律：`PGPASSWORD` 環境變數（**不進 argv**；`sync_to_postgres.py` 的 `subprocess.run()` 沒傳 `env=`，子行程直接繼承 `os.environ`，順帶繞開密碼含 `@ : / ? #` 的 percent-encoding 陷阱）、`PGSSLMODE=require`（libpq 預設 `prefer` 會在談不成 TLS 時**靜默降級成明文**）、`PGCONNECT_TIMEOUT=15`。

## A6.1 Secrets 與 Variables 的切分

GitHub 有兩種設定儲存，差別是**安全邊界**不是方便性：

- **Secret** —— GitHub 會在 log 中自動遮蔽成 `***`，只要值與已註冊的 secret 完全相符就會被遮，即使不是經由 `secrets.X` 引用的。
- **Variable**（`vars.X`）—— **完全不遮蔽**。而且 Actions 預設會把 `run:` 的指令回顯進 log，所以只要用到就等於印出來。

**本 repo 是 public，workflow log 全世界可讀**，所以判準是：值出現在公開 log 裡會不會造成問題。

| 類型 | 名稱 | 理由 |
|---|---|---|
| Secret | `SUPABASE_WRITER_PASSWORD` | 憑證 |
| Secret | `SUPABASE_READER_PASSWORD` | 憑證 |
| Secret | `SUPABASE_WRITER_URL`（**不含密碼**） | 本身不是憑證，但 Supabase pooler 的使用者名稱是 `gym_writer.<project-ref>`，等於把 ref 公開。放 secret 零成本 |
| Secret | `SUPABASE_READER_URL`（**不含密碼**） | 同上 |
| Variable | `PG_PORT` | A6 的實測結果（5432 或 6543）。放這裡就能不改 code 切換 |
| Variable | `PG_SSLMODE` | 固定 `require`，但顯式可見比藏起來好 |
| Variable | `EXPECT_VENUES` | `健身中心,室內游泳池`。NTU 若新增場館可直接改，不必動 code |
| Variable | `PUBLISH_DAYS` | panel 3 的視窗天數，預設 7 |

Owner 憑證**不進 GitHub**。migration、backfill 與需要 `DO UPDATE` 的修補都在本機手動執行，CI 永遠拿不到能 DROP 的權限。

**URL 裡不要寫 port，也不要寫 `sslmode`。** libpq 的優先序是「URL 裡的顯式值 > 環境變數 > 預設」，所以只要 URL 寫成 `...:5432/postgres`，`PGPORT` 這個 variable 就完全沒有作用——會變成一個看起來能切換、實際上不會生效的旋鈕。`sslmode` 同理。存成：

```text
postgresql://gym_writer.<project-ref>@aws-1-ap-northeast-1.pooler.supabase.com/postgres
```

port 與 sslmode 交給 `PGPORT` / `PGSSLMODE`。這樣 A6 的實測結果（5432 還是 6543）真的只要改一個 variable。

設定方式：
```bash
gh secret set SUPABASE_WRITER_PASSWORD    # 互動輸入，不進 shell history
gh secret set SUPABASE_WRITER_URL         # 不含密碼、不含 port、不含 sslmode
gh variable set PG_PORT --body 5432
gh variable set PG_SSLMODE --body require
gh variable set EXPECT_VENUES --body '健身中心,室內游泳池'
gh variable set PUBLISH_DAYS --body 7
```

三個必須先知道的限制：

1. **`on:` 區塊不能用任何 context。** cron 排程只能寫死在 YAML，`vars` / `secrets` 都無效。
2. **遮蔽不是安全邊界。** 對 secret 做 base64、反轉或切段後 echo 就能繞過。永遠不要印出來，也不要在 debug 模式下試。
3. **把整數性檢查設成 variable 有代價** —— 誰改了 `EXPECT_VENUES` 就等於關掉那道檢查，而且不會留下 code review 紀錄。這是刻意接受的取捨：加場館的頻率遠低於它帶來的便利。

`\getenv`（`003_roles_grants.sql`）與 `PGPASSWORD`（`sync_to_postgres.py`）對兩種來源都適用，因為 secret 和 variable 最後都只是環境變數，SQL 與 Python 都不需要知道值從哪來。

## A7. 本機 Grafana datasource

[grafana/provisioning/datasources/datasources.yml:20](grafana/provisioning/datasources/datasources.yml#L20) 目前是 `sslmode: disable`。指向 Supabase 時改成 `require`，帳號改用 `gym_reader`。dashboard JSON 不動（UID 仍是 `gym-postgres`）。

## A8. 把 DB 狀態版本化

RLS、`security_invoker`、grants、policies 全部寫成 migration 檔，不要留成 SQL Editor 的手動步驟 —— 否則 restore drill 重建不出來，也無從 review：

```
sql/migrations/001_schema.sql          # 現有 sql/init/01_schema.sql 的內容
sql/migrations/002_views.sql           # 現有 sql/grafana_weekly_views.sql
sql/migrations/003_roles_grants.sql    # A2
sql/migrations/004_rls_policies.sql    # A3
tools/apply_migrations.sh              # 依序 psql -v ON_ERROR_STOP=1 -f
```

`sql/init/` 仍留給 Docker 的 `docker-entrypoint-initdb.d` hook 使用（只在空 data dir 的首次啟動生效），內容從 `001` 產生或以 symlink 指過去，避免兩份漂移。

---

# Phase B — Cutover

## B1. 煙霧測試（先只放 `workflow_dispatch`，不加 schedule）

整個計畫唯一無法在本機驗證的假設：**GitHub 的 Azure 出口 IP 打不打得到 `rent.pe.ntu.edu.tw`**。若 NTU 擋境外流量，後面全部白做。

推一個只有 `workflow_dispatch` 的 workflow，用 `force=true` + `--allow-stale-source` 手動觸發，確認：

1. 能 parse 到正好 2 筆
2. `make_ssl_context()`([fetch_counts.py:50-58](fetch_counts.py#L50-L58)) 的 `VERIFY_X509_STRICT` workaround 在 Ubuntu 24.04 的 OpenSSL 3.0.x 下仍有效（這段是 intentional，不要動）
3. `gym_writer` 憑證能成功寫入，且 A1 的交易內驗證通過
4. Transaction pooler :6543 的 COPY 是否正常（A6）

**記下這幾筆的精確 `(fetched_at, venue)`** —— B2 的驗證要排除它們。

## B2. Backfill 13,020 列

前置：migration 全部套用完畢，且 collect workflow 尚未啟用。用 **owner 憑證 + `--on-conflict update`**（backfill 需要 `DO UPDATE`，且要繞過 A3 的一小時時間窗）。

```bash
export PGPASSWORD='<owner-pw>'; export PGSSLMODE=require
python3 sync_to_postgres.py --sqlite-db gym_counts.sqlite3 --on-conflict update \
  --postgres-url 'postgresql://postgres.<ref>@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres'
```

A0 救回的 2,444 列一併送入（可先合進 SQLite 再一次 backfill，或獨立跑第二次）。

**驗證分區間查，不查全表總數** —— B1 已經寫進去若干筆，全表 count 不會是任何預期值：

```sql
-- 原始 SQLite 區間
SELECT count(*) FROM occupancy
WHERE fetched_at <= '2026-06-21T09:50:00+08:00';                  -- 期望 13020

-- A0 救回的區間
SELECT count(*) FROM occupancy
WHERE fetched_at BETWEEN '2026-08-01T09:00:00+08:00'
                     AND '2026-08-07T21:55:00+08:00';             -- 期望 2444

-- 重建列可由 source_updated_at IS NULL 精確識別
SELECT count(*) FROM occupancy WHERE source_updated_at IS NULL;   -- 期望 2444

SELECT min(fetched_at AT TIME ZONE 'Asia/Taipei'),
       max(fetched_at AT TIME ZONE 'Asia/Taipei')
FROM occupancy WHERE source_updated_at IS NOT NULL;
-- 期望 2026-05-14 14:02:00 | 2026-06-21 09:50:00
```

另外單獨確認 B1 那幾筆的精確 key 存在。通過後才執行 A4 的第一次 dump + restore drill。

## B3. `.github/workflows/collect.yml`

```yaml
name: collect

on:
  schedule:
    # 台北時間。minute offset 到 :03 避開 GitHub 整點壅塞（官方明列整點為高負載時段）
    - cron: "3-59/5 6-21 * * 1-5"    # Mon-Fri 06:03-21:58
      timezone: "Asia/Taipei"
    - cron: "3-59/5 9-21 * * 6"      # Sat     09:03-21:58
      timezone: "Asia/Taipei"
    - cron: "3-59/5 9-17 * * 0"      # Sun     09:03-17:58
      timezone: "Asia/Taipei"
  workflow_dispatch:
    inputs:
      force:
        description: "Fetch even outside opening hours (smoke test)"
        type: boolean
        default: false

permissions:
  contents: read

concurrency:
  group: collect
  cancel-in-progress: false

jobs:
  collect:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    env:
      TZ: Asia/Taipei
      SQLITE_DB: ${{ runner.temp }}/gym_counts.sqlite3
      # secrets: 憑證與含 project-ref 的 URL。variables: 公開無妨的旋鈕。見 A6.1
      PG_URL:     ${{ secrets.SUPABASE_WRITER_URL }}      # 不含密碼
      PGPASSWORD: ${{ secrets.SUPABASE_WRITER_PASSWORD }}
      PGPORT:     ${{ vars.PG_PORT }}
      PGSSLMODE:  ${{ vars.PG_SSLMODE }}
      PGCONNECT_TIMEOUT: "15"
    steps:
      # runner 已內建 Python 3.12.3 / psql 16.14 / sqlite3 3.45.1
      # → 不需要 setup-python，也不需要 apt-get install postgresql-client
      - uses: actions/checkout@v6

      - name: Fetch
        run: |
          set -euo pipefail
          args=(--db "$SQLITE_DB" --no-log-file --expect-venues "${{ vars.EXPECT_VENUES }}")
          if [ "${{ inputs.force }}" = "true" ]; then
            args+=(--allow-stale-source)      # 閉館時來源時間戳本來就會過期
          else
            args+=(--open-hours-only)
          fi
          python3 fetch_counts.py "${args[@]}"

      - name: Sync
        run: |
          set -euo pipefail
          # connect() 在 open-hours 檢查(303)之前就跑，閉館時檔案仍會被建立、只是沒有列。
          # 一定要用列數判斷，不能用檔案是否存在。
          rows=$(sqlite3 "$SQLITE_DB" "SELECT COUNT(*) FROM occupancy;" 2>/dev/null || echo 0)
          [ "$rows" -eq 0 ] && { echo "closed hours - nothing to sync"; exit 0; }
          [ "$rows" -eq 2 ] || { echo "::error::expected 2 rows, got $rows"; exit 1; }
          # A1 的交易內驗證使 retry 冪等：重跑會命中 conflict 插入 0 列，但 JOIN 驗證仍會通過
          for a in 1 2 3; do
            python3 sync_to_postgres.py --sqlite-db "$SQLITE_DB" --postgres-url "$PG_URL" && exit 0
            echo "sync attempt $a/3 failed; sleeping $((a*10))s" >&2; sleep $((a*10))
          done
          echo "::error::sync failed 3 times - this reading is lost"; exit 1

      - name: Preserve unsent reading
        if: failure()
        uses: actions/upload-artifact@v7
        with:
          name: unsent-${{ github.run_id }}
          path: ${{ runner.temp }}/gym_counts.sqlite3
          retention-days: 7
```

**寫入是否成功不再靠 workflow 層的 freshness 查詢判斷** —— 那只能證明「資料庫最近有某筆資料」，前一個 run 剛成功時會給出假綠燈。改由 A1 的交易內逐筆驗證負責，失敗即 `RAISE EXCEPTION` → psql 非零退出 → 觸發 retry。少一個步驟、少一條連線、語意還更強。

其餘決策：

- **`actions/checkout@v6`**（原提案的 `@v7` 不存在）、**`actions/upload-artifact@v7`**（目前最新為 v7.0.1）。
- **`3-59/5`** 產生 3,8,…,58，每小時滿 12 次，避開 minute 0。相較舊 cron 只少了每天 06:00–06:02。
- **`concurrency` 的真實語意**：同 group 只保留**一個** pending run，新的取代舊的 pending。所以「不會重疊」成立，「每次排程都會被保留」**不成立** —— 若某個 run 卡住，後續採集會被丟掉。這是 `timeout-minutes: 5` 必須短的理由。
- **SQLite 是拋棄式的，讀數只有一次機會。** `.gitignore:16-17` 忽略 `*.sqlite3`，runner 上永遠是空檔。好處是 `read_sqlite_rows()` 沒有 WHERE clause 這件事自動不再是問題（只送 2 列而非 13,020 列）；壞處是 sync 失敗該筆永久消失，所以有 3 次重試 + 失敗存 artifact。
- **fetch 失敗讓 job 紅掉。** 歷史日誌顯示 6,510 次 fetch 中三次嘗試全失敗約 6–7 次（≈0.1%），以每天 ~192 次算大約每 5 天一個紅叉。這是唯一的告警管道。
- **`--no-log-file`**：runner 是 ephemeral 且 `logs/` 被 gitignore；順帶避開 `DailyLogStream` 缺 `isatty()` / `fileno()` / `encoding` 的隱患。

## B4. `.github/workflows/publish.yml`（GitHub Pages）

Repo Settings → Pages → Source 設為 **GitHub Actions**。網址 `https://uranustrong.github.io/NTU_Gym_Traffic/`。

```yaml
name: publish

on:
  schedule:
    - cron: "8-53/15 6-21 * * 1-5"
      timezone: "Asia/Taipei"
    - cron: "8-53/15 9-21 * * 6"
      timezone: "Asia/Taipei"
    - cron: "8-53/15 9-17 * * 0"
      timezone: "Asia/Taipei"
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  publish:
    runs-on: ubuntu-24.04
    timeout-minutes: 8
    environment:
      name: github-pages
      url: ${{ steps.deploy.outputs.page_url }}
    env:
      TZ: Asia/Taipei
      PG_URL:     ${{ secrets.SUPABASE_READER_URL }}
      PGPASSWORD: ${{ secrets.SUPABASE_READER_PASSWORD }}
      PGPORT:     ${{ vars.PG_PORT }}
      PGSSLMODE:  ${{ vars.PG_SSLMODE }}
      PGCONNECT_TIMEOUT: "15"
    steps:
      - uses: actions/checkout@v6          # 必要：script、dashboard JSON、template、echarts 都在 repo 裡
      - run: |
          python3 tools/render_public_html.py --output-dir _site \
            --postgres-url "$PG_URL" --days "${{ vars.PUBLISH_DAYS }}"
      - uses: actions/upload-pages-artifact@v5
        with:
          path: _site
      - id: deploy
        uses: actions/deploy-pages@v5
```

- **15 分鐘一次，不是 5 分鐘。** Pages 的「每小時 10 build」soft limit 不適用於自訂 Actions 發佈，但每次要上傳 1 MB 的 `tools/echarts.min.js` 加內嵌資料的 HTML，5 分鐘一次純浪費。
- `--postgres-url` **必須給**：`load_env()`([tools/render_public_html.py:47-62](tools/render_public_html.py#L47-L62)) 在沒有 `.env` 時回傳 `{}`（`.env` 被 gitignore），不給會 fallback 到 `127.0.0.1:5433` 然後失敗。
- [tools/render_public_html.py:327](tools/render_public_html.py#L327) 的 `dt.datetime.now().astimezone()`（`GENERATED_AT`）一併改用 `ZoneInfo`。
- **「every 5 min」文案要改三處**：[tools/public_template.html:6](tools/public_template.html#L6) 的 `<meta http-equiv="refresh" content="300">` → `900`，以及 [:69](tools/public_template.html#L69) 與 footer 的文字。
- 需要**兩個 view 都存在**（[:127](tools/render_public_html.py#L127)、[:158](tools/render_public_html.py#L158)、[:215](tools/render_public_html.py#L215)）。

## B5. 對抗 60 天自動停用

**workflow run 本身不算 repository activity** —— 每天 192 次 collect 完全不會重置那個 60 天計時器，只有 commit / issue / PR 才算。本 repo 上次 push 是 2026-05-21，已 80 天。

在 A4 的 `backup.yml` 附掛（`permissions: contents: write`）：

```yaml
      - run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git commit --allow-empty -m "chore: keepalive $(date -u +%FT%TZ)"
          git push
      - env: {GH_TOKEN: "${{ github.token }}"}
        run: gh api -X PUT "repos/${{ github.repository }}/actions/workflows/collect.yml/enable" || true
```

- **branch protection 會擋住 `GITHUB_TOKEN` 的 push。** 若 `main` 有保護規則需改用 PAT 或推到非保護分支。**部署後必須實際確認 push 成功**，不能假設。
- `GITHUB_TOKEN` 推的 commit 不觸發其他 workflow（防遞迴），但確實是 commit，計時器會重置。
- `/enable` 是保險；若 backup.yml 自己也被停用就失效，所以主要防線是每週那個 commit。**定期確認 collect workflow 仍是 enabled。**
- 連鎖風險：Actions 被停用 → 7 天後 Supabase Free 專案因低活動被 pause → 需手動 unpause。

## B6. ws7 收尾（已確認幾乎不需要動作）

已實地確認：ws3 / ws7 皆 `no crontab for b12902066`、`/var/spool/cron/crontabs/` 不存在、`~/public_html/gym/` 不存在、home 下無 Gym_Fetch 殘留。**沒有殘留的排程或 process，不需要 `crontab -r`，也沒有雙寫風險。**

唯一還活著的是**那個公開頁**。已確認實際位置是 `~/htdocs/gym/`（不是文件寫的 `~/public_html` —— CSIE 的 UserDir 根目錄是 `htdocs`，這個錯誤要一併修進 D3）：

```
-rw-r--r-- 1 b12902066 student 1024740  5月 20 13:13 echarts.min.js
-rw-r--r-- 1 b12902066 student  166203  8月  8 03:29 index.html
```

`index.html` 的 mtime 精確標定了 render 停止的時刻。

處置順序：
1. **A0 完成前不要動它。** 它是那 2,444 列的唯一來源。
2. A0 的 HTML 已存進 repo、資料已進 Supabase 之後，B4 上線，再把 `index.html` 換成導向 GitHub Pages 的 redirect（比刪掉好 —— 保留既有連結）。`echarts.min.js` 屆時可一併刪除。

**不要放著不管** —— 一個持續服務舊資料、又標示「auto-refreshes every 5 min」的頁面，比 404 更誤導。

repo 側同步標示 deprecated：[rsync.sh](rsync.sh)、[tools/sync_local.sh](tools/sync_local.sh)、[tools/psql_gym_postgres.sh](tools/psql_gym_postgres.sh)、[crontab.example](crontab.example)。

---

# Phase C — 上線後量測

## C1. 誠實的預期

GitHub 文件明說 schedule「在高負載期間可能延遲」且「部分排隊的 job 可能被丟棄」，高負載包含**每個整點**。務實預期是 **85–95% 送達率**，且 `fetched_at` 不再落在 5 分鐘網格上（目前 13,014/13,020 落在 `minute % 5 == 0`）。

**降級而非損壞，但不要說得太滿。** `fetched_at` 是每列自帶的真實時間戳，保留真實值是正確決定。但要承認兩件事：

1. **延遲會在 30 分鐘桶邊界重新分配樣本。** 名目 `:28` 或 `:58` 的 run 只要延遲兩分鐘就跨進下一個桶 —— 這不只是「sample_count 變小」，而是相鄰兩個 bucket 之間的樣本被搬動。
2. **缺失未必是隨機的。** GitHub 的壅塞集中在整點附近，所以掉的 run 會系統性偏向某些 slot。這正是 C2(c) 要按 slot 檢查覆蓋率、而不是只看每日總數的原因。

在 30 分鐘桶、每桶 6 筆的粒度下，這些效應對 `AVG` / median 的影響很小，但它是偏差不是純雜訊。

## C2. 一週後的量測

平日期望 192、週六 156、週日 108。四個指標都要看：

```sql
-- (a) 每日送達率
SELECT (fetched_at AT TIME ZONE 'Asia/Taipei')::date AS day,
       count(*) FILTER (WHERE venue='健身中心') AS delivered,
       CASE EXTRACT(ISODOW FROM fetched_at AT TIME ZONE 'Asia/Taipei')
            WHEN 6 THEN 156 WHEN 7 THEN 108 ELSE 192 END AS expected
FROM occupancy WHERE fetched_at >= now() - interval '7 days'
GROUP BY 1, EXTRACT(ISODOW FROM fetched_at AT TIME ZONE 'Asia/Taipei') ORDER BY 1;

-- (b) 間隔分位數與最大缺口。
--     跨夜缺口以「是否同一個台北日期」排除，不可用 gap 大小排除
--     （按大小過濾會把真正的長時間 outage 一起藏掉）。
WITH r AS (
  SELECT (fetched_at AT TIME ZONE 'Asia/Taipei') AS local_ts,
         EXTRACT(EPOCH FROM (fetched_at - lag(fetched_at) OVER w))/60 AS gap,
         lag(fetched_at AT TIME ZONE 'Asia/Taipei') OVER w AS prev_local
  FROM occupancy
  WHERE venue = '健身中心' AND fetched_at >= now() - interval '7 days'
  WINDOW w AS (ORDER BY fetched_at)
)
SELECT round(percentile_cont(0.50) WITHIN GROUP (ORDER BY gap)::numeric,2) AS p50,
       round(percentile_cont(0.90) WITHIN GROUP (ORDER BY gap)::numeric,2) AS p90,
       round(percentile_cont(0.99) WITHIN GROUP (ORDER BY gap)::numeric,2) AS p99,
       round(max(gap)::numeric,2) AS max_gap,
       count(*) FILTER (WHERE gap > 10) AS gaps_over_10min
FROM r
WHERE gap IS NOT NULL AND local_ts::date = prev_local::date;   -- 只排除跨夜

-- (c) 最近七天、每個 venue × 台北日期 × 30 分鐘 slot 的實際筆數（正常應為 6）。
--     不要用 weekly_occupancy_slots —— 它是全歷史累積、沒有日期維度。
SELECT venue,
       (fetched_at AT TIME ZONE 'Asia/Taipei')::date AS local_date,
       make_time(EXTRACT(HOUR   FROM fetched_at AT TIME ZONE 'Asia/Taipei')::int,
                 (floor(EXTRACT(MINUTE FROM fetched_at AT TIME ZONE 'Asia/Taipei')/30)*30)::int, 0) AS slot,
       count(*) AS n
FROM occupancy WHERE fetched_at >= now() - interval '7 days'
GROUP BY 1,2,3 HAVING count(*) < 5 ORDER BY 2,3,1;   -- 只列出覆蓋不足的 slot

-- (d) source_updated_at 重複讀數（延遲會讓兩個 run 落在同一個來源 5 分鐘窗口，造成雙重計數）
SELECT count(*) FROM (
  SELECT venue, source_updated_at FROM occupancy
  WHERE source_updated_at IS NOT NULL AND fetched_at >= now()-interval '7 days'
  GROUP BY 1,2 HAVING count(*) > 1) d;
```

**決策門檻**：p50 ≤ 6 分鐘、送達率 ≥ 90%、且 (c) 沒有系統性偏向某些 slot → 維持。任一不達標 → 改成每 10 分鐘（`3-59/10`），用穩定的 10 分鐘換掉不穩定的 5 分鐘；30 分鐘桶下資料品質幾乎不受影響，run 數砍半。延遲嚴重到不可接受 → 見 D5。

---

# Phase D — Follow-up（不在本次範圍）

1. **第四個星期一過濾（只改 view 層）。** 技巧：任何星期幾的第 4 次出現必定落在該月 **22–28 日**，不需要 calendar table。在 [sql/grafana_weekly_views.sql](sql/grafana_weekly_views.sql) 的 `open_slots` CTE 加 `AND NOT (weekday_number = 1 AND EXTRACT(DAY FROM local_fetched_at)::int BETWEEN 22 AND 28 AND slot_start_time < TIME '17:00')`（需先把 `local_fetched_at` 從 `localized` 往下傳到 `slotted`），同條件同步到 [tools/render_public_html.py:185-187](tools/render_public_html.py#L185-L187)。`grafana/weekly-dashboard.json:150` 的 `OPEN_HOURS_MIN` **不用改**（heatmap 軸是「星期×時段」，本質表達不了「某月的第四個週一」）。`fetch_counts.py` 也不動。長久解是建 `closures(starts_at, ends_at, venue, reason)` table 用 anti-join，第四個週一當第一筆種子。

2. **`source_updated_at` 去重**（C2(d) 顯示有重複才做）：INSERT 加 anti-join guard + `CREATE INDEX ix_occupancy_venue_src ON occupancy (venue, source_updated_at)`。

3. **文件收尾。** （`docs/SERVER_SETUP.md` 的 UserDir 已由 commit `3666b87` 於 2026-05-21 修正為 `~/htdocs`，不需再處理；但它整份描述的仍是已不存在的 ws7 部署。）[README_Server.md:48-62](README_Server.md#L48-L62) 與 [README_Local.md:50-64](README_Local.md#L50-L64) 仍寫平日 `08:00-22:00` 與 `*/5 8-21`，且 README_Local 的路徑還是舊的 `Fetch_Gym`；這兩份描述的部署方式遷移後已不存在，應改寫而非小修。`CLAUDE.md:69` 的「duplicated in three places」要改成 **four**（漏了 `tools/render_public_html.py:sql_panel3`）。`git remote set-url origin git@github.com:Uranustrong/NTU_Gym_Traffic.git`（目前還是舊名 `Fetch_Gym`，靠 GitHub redirect）。

4. **個人網站接法**：讓瀏覽器透過 PostgREST + anon key 直接查 Supabase（配合 A3 的 `security_invoker` + 唯讀 policy），頁面只 deploy 一次就永遠即時，可完全取代 15 分鐘重新渲染。

5. **若 GH 節奏最終不可接受**：Supabase Edge Function + `pg_cron`／`pg_net` 可在 Supabase 內部跑完整條 pipeline —— 1 分鐘 granularity、無延遲/丟棄、無 60 天停用、無跨服務 secret。代價是把 `parse_counts()`([fetch_counts.py:76-110](fetch_counts.py#L76-L110)) 移植到 Deno/TS（約 40 行，regex 可沿用），主要未知數是 `VERIFY_X509_STRICT` workaround 在 Deno（rustls，非 OpenSSL）下是否還需要／是否可行。**先跑滿一週拿 C2 的實測數字再決定。**

---

# Verification

| # | 動作 | 驗證 |
|---|---|---|
| 0 | A0 搶救 | 原始 HTML 已存進 `docs/recovery/`；重建 CSV 為 **2,444 列**、範圍 `2026-08-01T09:00+08` → `2026-08-07T21:55+08`、每日筆數 312/216/384/384/380/384/384、兩 venue 各 1,222 |
| 1 | A1 重構 | `python3 -m unittest test_sync_to_postgres.py` —— `call_count` 改 1、psql 非零退出不得回報成功、驗證失敗須 rollback |
| 2 | A5.4 | `python3 -m unittest test_fetch_counts.py` —— 12 個開關館邊界 + `TZ=UTC` case |
| 3 | A8 migration | `./tools/apply_migrations.sh` 對**乾淨的本機 Docker Postgres** 從零跑完不報錯 |
| 4 | A3 權限 | anon key 打三個 PostgREST 物件皆 401/403；`gym_writer` 可 INSERT、不可 UPDATE/DELETE、超出一小時窗口的 INSERT 被 policy 擋下；`gym_reader` 可 SELECT 不可 INSERT |
| 5 | B1 煙霧 | 手動 dispatch → 正好 2 列 → A1 交易內驗證通過；記下精確 key；:6543 的 COPY 通過與否 |
| 6 | B2 backfill | `count(*) WHERE fetched_at <= '2026-06-21T09:50+08'` = 13020；A0 區間 = 2444；`source_updated_at IS NULL` = 2444；B1 的 key 另行確認存在 |
| 7 | A4 備份 | data-only dump → 本機空 DB 先套 migration 再 `pg_restore --data-only` → count 相符 |
| 8 | B3 排程 | **在開館時間內**觀察連續 3 次自動 run（閉館時手動 dispatch 會 exit 0 且同步 0 列、整個 workflow 綠燈，不構成驗證） |
| 9 | B4 Pages | 四個 panel 有資料、venue/palette/granularity 切換正常、`echarts.min.js` 無 404 |
| 10 | B5 keepalive | 手動觸發 backup.yml，確認 empty commit **實際 push 成功**（branch protection 會擋） |
| 11 | C2 | 一週後跑四個量測 query，據門檻決定維持 5 分鐘或降到 10 分鐘 |

`test_grafana_weekly_views.py` 會建立再 drop `grafana_weekly_test` schema —— 繼續對本機 Docker Postgres 跑，**不要**指向 Supabase production。

本機 36 個 `.bak-*` 檔（約 12 MB）在第 6、7 步都通過後才可清除。
