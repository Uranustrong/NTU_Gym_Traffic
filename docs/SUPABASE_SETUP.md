# Supabase setup — the hand-off checklist

The design lives in [`docs/plan/2026-08-09-github-actions-supabase-migration.md`](plan/2026-08-09-github-actions-supabase-migration.md). This is the do-it list: what you click, what you hand over, and what happens next.

Steps 1–5 are yours because they need an account nobody else can log into. Everything from step 6 on is automated.

---

## What you need before starting

- A Supabase account (GitHub login is fine).
- `psql` on your Mac — you have 18.3 from Homebrew, which is fine.
- Roughly 10 minutes, most of it waiting for the project to provision.

Free plan limits worth knowing up front: **500 MB** database, **2 active projects**, and a project is **paused after 7 days without database activity**. At ~127k rows a year this project fits inside 500 MB for about a decade, and a 5-minute write cadence never comes close to the pause threshold. But if collection ever stops for a week, the project pauses and needs manual reactivation.

---

## 1. Create the project

[supabase.com/dashboard](https://supabase.com/dashboard) → **New project**.

| Field | Value | Why |
|---|---|---|
| Name | `ntu-gym-traffic` | Anything; it is only a label |
| Database Password | **generate a strong one and save it** | This is the password for the `postgres` owner role. It is shown once |
| Region | **Northeast Asia (Tokyo)** or **Southeast Asia (Singapore)** | Latency barely matters for two rows every five minutes, but it makes your own queries snappier |
| Plan | Free | |

**Save the password before clicking create.** Supabase will not show it again, and recovering from a reset means redoing the role setup.

Provisioning takes a couple of minutes.

## 2. Note the project ref

Once the dashboard opens, the URL is:

```
https://supabase.com/dashboard/project/abcdefghijklmnop
                                       ^^^^^^^^^^^^^^^^ this is the project ref
```

Copy it. It appears in every connection string.

## 3. Get the Session pooler host

Click **Connect** at the top of the dashboard. You will see several options:

| Option | Port | Use |
|---|---|---|
| Direct connection | 5432 | IPv6-only; skip it |
| **Session pooler** | **5432** | **What we start with** |
| Transaction pooler | 6543 | Tested later in A6; may become the choice for the workflows |

Select **Session pooler** and copy just the hostname — it looks like `aws-1-ap-northeast-1.pooler.supabase.com`. Ignore the rest of the string; we assemble the URL ourselves for a reason explained below.

## 4. Fill in the local credentials file

```bash
cd /Users/songhejun/Downloads/My_Project/NTU_Gym_Traffic
cp supabase.secrets.example supabase.secrets
chmod 600 supabase.secrets
$EDITOR supabase.secrets
```

Fill in exactly three values — `SUPABASE_PROJECT_REF`, `SUPABASE_POOLER_HOST`, `PGPASSWORD`. Everything else is derived, and the two role passwords get generated in step 6.

The file is gitignored (`*.secrets`) and is designed to be **sourced, never read**. Tooling picks the values out of the environment; they do not need to be pasted into a chat, a command line, or a workflow file.

> **Why the URLs in that file have no port and no `sslmode`.** libpq resolves settings in the order *explicit URL value → environment variable → default*. Baking `:5432` into the URL would make `PGPORT` a knob that looks adjustable and silently does nothing — which matters because deciding between 5432 and 6543 is exactly what step 9 does.

## 5. Confirm it works, then tell me

```bash
bash -c 'set -a; . ./supabase.secrets; set +a; psql "$SUPABASE_OWNER_URL" -X -A -t -c "SELECT current_database(), current_user, version();"'
```

Expected: one line naming `postgres`, `postgres`, and a PostgreSQL 15/17 banner. If it fails:

| Symptom | Cause |
|---|---|
| `could not translate host name` | `SUPABASE_POOLER_HOST` has a stray port or `https://` |
| `password authentication failed` | Wrong password, or the ref suffix is missing from the username — through the pooler it must be `postgres.<ref>`, not `postgres` |
| `Connection refused` | Wrong port; leave `PGPORT=5432` for now |
| Hangs then times out | Project is still provisioning, or it is paused — check the dashboard |

**Then say "Supabase 好了" and I take it from there.** I never read `supabase.secrets`; commands source it inside a subshell so the values stay out of the transcript.

---

## What I do next (steps 6–11)

| # | Step | What it proves |
|---|---|---|
| 6 | Generate `GYM_WRITER_PASSWORD` / `GYM_READER_PASSWORD` into `supabase.secrets` | Strong, distinct, never typed by hand |
| 7 | `tools/apply_migrations.sh` — table, views, roles, RLS | Rebuildable from an empty database |
| 8 | Verify least privilege | writer cannot UPDATE, DELETE, or backdate; reader cannot write; anon gets 401 from the REST API |
| 9 | **A6**: run a real sync over both 5432 and 6543 | Decides `PG_PORT`; `COPY ... FROM STDIN` through the transaction pooler is the open question |
| 10 | Backfill **15,464** rows and verify by range | 13,020 original + 2,444 recovered, `source_updated_at IS NULL` = 2,444 |
| 11 | **A4**: data-only `pg_dump` as `gym_reader`, restore into a local throwaway database | The backup is rehearsed, not assumed |

Then I hand you the exact `gh secret set` / `gh variable set` commands for step 12, with `PG_PORT` already set to whatever step 9 measured.

---

## What you do after (step 12)

Set the GitHub secrets and variables. The split and the reasoning are in section A6.1 of the plan; the short version is that **secrets are masked in logs and variables are not**, and this repo is public, so its workflow logs are world-readable.

I will give you the commands with values filled in. You run them, because they should not pass through anything but your own terminal:

```bash
gh secret set SUPABASE_WRITER_PASSWORD    # prompts, so nothing lands in shell history
gh secret set SUPABASE_READER_PASSWORD
gh secret set SUPABASE_WRITER_URL
gh secret set SUPABASE_READER_URL
gh variable set PG_PORT --body <from step 9>
gh variable set PG_SSLMODE --body require
gh variable set EXPECT_VENUES --body '健身中心,室內游泳池'
gh variable set PUBLISH_DAYS --body 7
```

**The owner password never goes into GitHub.** Migrations, backfills and anything needing `ON CONFLICT DO UPDATE` run from your Mac. CI holds credentials that can append readings and read them back, and nothing else — so the worst case for a leaked workflow secret is junk rows in the last hour, not a dropped table.

After that, Phase B starts: the collector workflow, then GitHub Pages, then backups.

---

## Things that will bite if nobody says them

- **The project ref is in the username, not just the host.** Through Supavisor every role is `<role>.<project-ref>` — `postgres.abcdef…`, and after step 7 also `gym_writer.abcdef…`. Omitting the suffix produces an authentication failure that reads like a wrong password.
- **`ALTER ROLE … PASSWORD` is how role passwords are set**, and migration 003 runs it every time. Re-running `apply_migrations.sh` with new values in `supabase.secrets` is therefore how you rotate them — no dashboard involved.
- **Tables in `public` are exposed through PostgREST by default**, and the anon key that reaches them is public by design. Migration 004 revokes that and switches both views to `security_invoker`, because a view owned by a high-privilege role otherwise executes as its owner and reads straight past the table's RLS. Step 8 verifies this rather than trusting it.
- **Free plan has no automatic backup.** That is not a footnote here: this project has already lost roughly 48% of its history to a wiped scratch directory. Step 11 exists so there is a rehearsed restore before Supabase becomes the only copy.
