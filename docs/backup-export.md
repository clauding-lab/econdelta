# Off-box history export (E2.4)

`data/` is git-ignored and lives only on ExonVPS. The irreplaceable history —
`metric_history_monthly`'s hand-verified fiscal backfill (AGENTS.md landmine 32)
and the LLM-extracted / static-tier rows in `metric_history` — is **not
re-scrapable**. Supabase is the single off-box copy, so a Supabase loss would be
a permanent data loss. `scripts/export_history.py` writes those tables to a
portable, timestamped JSON file so the history survives.

## What it exports

- `metric_history_monthly` — full.
- `metric_history` — full by default; with `--irreplaceable-only` it drops the
  re-scrapable daily market series (DSE index/tickers, forex, commodity) for a
  lean, committable snapshot.

Output: `econdelta_history_export_<YYYY-MM-DD>.json` with an `exported_at`
timestamp, a per-table `manifest` row count, and the rows themselves.

## Run it

Needs the Supabase URL + a read key in the environment (service-role on the box;
the anon key also works for the anon-readable tables):

```bash
SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
  python -m scripts.export_history --out-dir /var/backups/econdelta
```

## Where to run it — OFF the box

Run it **off ExonVPS** so the backup doesn't share fate with the primary copy.

### Option A — weekly cron on Hetzner (recommended)

The Brief already runs on Hetzner `clauding-lab` and reads this Supabase project.
Add a weekly cron there (the repo is checked out at `~/econdelta` or clone it):

```cron
# 07:15 UTC every Monday — after the week's data has settled
15 7 * * 1  cd /home/adnan/econdelta && SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
  /home/adnan/econdelta/.venv/bin/python -m scripts.export_history \
  --out-dir /home/adnan/backups/econdelta >> /home/adnan/logs/econdelta-export.log 2>&1
```

Rotate/prune the `--out-dir` on your usual schedule (e.g. keep the last 12 weeks).

### Option B — git-tracked snapshot

For a version-controlled copy of just the irreplaceable tier:

```bash
python -m scripts.export_history --out-dir docs/snapshots --irreplaceable-only
git add docs/snapshots/econdelta_history_export_*.json && git commit -m "chore: history snapshot"
```

Prefer Option A for the routine backup (a multi-MB JSON committed weekly bloats
git); use Option B for occasional milestone snapshots.

> This is repo-side only. No ExonVPS install step is required — by design the
> export runs off-box.
