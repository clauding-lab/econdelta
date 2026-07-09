#!/usr/bin/env bash
# EconDelta — ordered pre-fetch deploy pull (E2.3).
#
# Runs ~04:50 BDT (22:50 UTC) as the service user, BEFORE the 05:00 BDT fetch
# cascade (econdelta-fetch @ 23:00 UTC), so a merged fix is live for that day's
# run instead of drifting until a manual `ssh git pull` (merge != deploy — the
# the-brief landmine-21 class). ONE ordered pull, not 16 per-unit ExecStartPre
# pulls that could swap code mid-cascade.
#
# Deliberately UNPRIVILEGED and ff-only:
#   * branch guard — refuses unless the checkout is on main, so an automated
#     pull can NEVER run feature-branch code unattended;
#   * --ff-only — never creates a merge commit; a diverged box fails loudly;
#   * on deploy/*.service|*.timer|install.sh changes it ALERTS a human rather
#     than auto daemon-reloading: a unit change already needs a manual
#     `sudo bash deploy/install.sh` (daemon-reload + enable new timers), and
#     auto-reloading a changed OnCalendar= risks a landmine-5 catch-up fire.
set -uo pipefail

REPO="${ECONDELTA_HOME:-/home/adnan-local/econdelta}"
PY="$REPO/.venv/bin/python"

log() { echo "[gitpull $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Best-effort Discord via the repo notifier (reads DISCORD_WEBHOOK_URL from env).
_notify() {
  "$PY" - "$1" "$2" "$3" <<'PYEOF' || true
import sys
from utils.notifier import notify
notify(sys.argv[1], sys.argv[2], sys.argv[3])
PYEOF
}

cd "$REPO" || { log "FATAL: repo $REPO not found"; exit 1; }

BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo DETACHED)"
if [[ "$BRANCH" != "main" ]]; then
  log "REFUSING: checkout is on '$BRANCH', not main — skipping pull."
  _notify warning "gitpull refused — not on main" \
    "ExonVPS checkout is on '$BRANCH', not main. Automated pull skipped so no feature-branch code runs unattended. Run 'git checkout main' on the box."
  exit 0
fi

BEFORE="$(git rev-parse HEAD)"
log "HEAD before: $BEFORE (branch main)"

if ! git pull --ff-only origin main >/tmp/econdelta-gitpull.out 2>&1; then
  OUT="$(cat /tmp/econdelta-gitpull.out 2>/dev/null)"
  log "PULL FAILED: ${OUT}"
  _notify error "gitpull failed" "git pull --ff-only origin main failed on ExonVPS: ${OUT:0:400}"
  exit 1
fi
while IFS= read -r l; do log "  $l"; done < /tmp/econdelta-gitpull.out

AFTER="$(git rev-parse HEAD)"
log "HEAD after:  $AFTER"

if [[ "$BEFORE" == "$AFTER" ]]; then
  log "already up to date — no deploy change."
  exit 0
fi
log "updated $BEFORE -> $AFTER"

CHANGED_UNITS="$(git diff --name-only "$BEFORE" "$AFTER" -- \
  'deploy/*.service' 'deploy/*.timer' 'deploy/*.service.d/*' 'deploy/install.sh')"
if [[ -n "$CHANGED_UNITS" ]]; then
  log "systemd unit / installer files changed — MANUAL 'sudo bash deploy/install.sh' required:"
  while IFS= read -r f; do log "    $f"; done <<< "$CHANGED_UNITS"
  _notify warning "gitpull: unit files changed — action needed" \
    "Pull $AFTER changed systemd units/installer. Run 'sudo bash deploy/install.sh' on ExonVPS to apply (daemon-reload + enable). Changed: $(echo "$CHANGED_UNITS" | tr '\n' ' ')"
fi
exit 0
