#!/usr/bin/env bash
# Weekly-briefing first deploy — run on ExonVPS as adnan-local.
# Safe to re-run. Aborts (non-destructively) if a prerequisite isn't met.
#
# Prerequisites (do these first — the box can't):
#   1. Merge PRs #41 (Opus-4.8 bump -> main) and #42 (box-compat config).
#   2. Apply db/migrations/0008_briefings.sql in the Supabase SQL editor
#      (the box has no psql / DATABASE_URL; PostgREST can't run DDL).
set -euo pipefail

REPO=/home/adnan-local/econdelta
ENVFILE=/etc/econdelta.env

echo "== Loading env (values not printed) =="
set -a; . "$ENVFILE"; set +a
: "${SUPABASE_URL:?missing}" "${SUPABASE_SERVICE_ROLE_KEY:?missing}"

echo "== 1. Get main onto the box =="
cd "$REPO"
git fetch origin --quiet
# Gate: main must already contain the Opus-4.8 bump (PR #41), else switching loses it.
if ! git show origin/main:claude_max/max_client.py | grep -q 'claude-opus-4-8'; then
  echo "ABORT: origin/main lacks the Opus-4.8 bump — merge PR #41 first."; exit 1
fi
git checkout main
git pull --ff-only

echo "== 2. Gate: briefings table must exist (you applied 0008 via the dashboard) =="
code=$(curl -s -o /dev/null -w '%{http_code}' \
  "$SUPABASE_URL/rest/v1/briefings?limit=1" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY")
[ "$code" = "200" ] || { echo "ABORT: briefings table not found (HTTP $code). Apply db/migrations/0008_briefings.sql in the Supabase SQL editor first."; exit 1; }
echo "OK: briefings table exists."

echo "== 3. Ensure BRIEFING_EFFORT=max in the live env (idempotent) =="
if ! grep -q '^BRIEFING_EFFORT=' "$ENVFILE"; then
  echo 'BRIEFING_EFFORT=max' | sudo tee -a "$ENVFILE" >/dev/null
  echo "Added BRIEFING_EFFORT=max"
else
  echo "Already set: $(grep '^BRIEFING_EFFORT=' "$ENVFILE")"
fi
set -a; . "$ENVFILE"; set +a   # reload

echo "== 4. Gate: verify model+effort resolve on this CLI (one cheap call) =="
.venv/bin/python - <<'PY'
import os, sys
from claude_max.max_client import run_max, MaxCallError
model  = os.environ.get("BRIEFING_MODEL", "opus[1m]")
effort = os.environ.get("BRIEFING_EFFORT", "max")
try:
    r = run_max(prompt="Reply with exactly: OK", model=model, effort=effort, timeout_s=90)
    print(f"OK: model={model} effort={effort} resolve (parsed={r.parsed!r})")
except MaxCallError as e:
    sys.stderr.write(f"ABORT: {e}\n")
    sys.stderr.write("Fix: set BRIEFING_MODEL=claude-opus-4-8 in /etc/econdelta.env, then re-run (see AGENTS landmine 21).\n")
    sys.exit(1)
PY

echo "== 5. Install the briefing unit + drop-in (NO timer arming yet — avoids landmine #5 catch-up) =="
sudo install -m 0644 deploy/econdelta-briefing.service /etc/systemd/system/
sudo install -m 0644 deploy/econdelta-briefing.timer   /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/econdelta-briefing.service.d
sudo install -m 0644 deploy/econdelta-briefing.service.d/*.conf /etc/systemd/system/econdelta-briefing.service.d/
sudo systemctl daemon-reload

echo "== 6. One deliberate smoke run =="
sudo systemctl start econdelta-briefing.service || true
sleep 3
echo "--- last 40 log lines ---"
tail -40 "$REPO/logs/briefing-systemd.log" 2>/dev/null || echo "(no log yet)"
echo "--- latest briefings row ---"
curl -s "$SUPABASE_URL/rest/v1/briefings?select=week_of,title,data_as_of,stale_series&order=week_of.desc&limit=1" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"; echo
echo "--- latest run_logs(source=briefing) ---"
curl -s "$SUPABASE_URL/rest/v1/run_logs?source=eq.briefing&select=started_at,status,duration_ms,error&order=started_at.desc&limit=1" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"; echo

cat <<'NOTE'

== Interpreting the smoke ==
 - status=ok  + a briefings row  -> live; YieldScope shows it on next load.
 - status=stale (no row)         -> the freshness gate correctly skipped (core data not fresh); not a failure.
 - status=fail                   -> check the log + run_logs.error above.

== Final step: arm the weekly timer when YOU'RE ready ==
   sudo systemctl enable --now econdelta-briefing.timer
   # Persistent=true: if the last Mon 01:00 UTC slot has passed, this fires one
   # catch-up run immediately (another briefing). Enable just after a Monday run,
   # or accept the one extra run.
   systemctl list-timers econdelta-briefing.timer
NOTE
echo "Done."
