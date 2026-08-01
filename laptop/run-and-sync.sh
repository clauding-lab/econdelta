#!/usr/bin/env bash
# EconDelta laptop-side scraper + rsync to VPS.
# Usage: run-and-sync.sh <scraper_name>
#   where <scraper_name> is a module in scrapers/ (e.g., bb_forex, dse_market)
#
# Invoked by launchd plists in ~/Library/LaunchAgents/.
# Sources secrets from ~/.econdelta.env (not committed).
#
# Behavior:
#   1. Activate venv
#   2. Run scraper module
#   3. If scraper exits 0 (success or non-trading-day no-op), rsync snapshot dir to VPS
#   4. If scraper exits 2 (anomaly hold), rsync anyway — bb_forex still writes a
#      snapshot on hold (fresh rates + previous reserves carried forward); a
#      scraper that skips the write entirely on exit 2 just gets a no-op rsync
#   5. If scraper exits 1 (hard error), log, do not rsync; scraper already fired Discord
#   6. Log all output to logs/launchd-<scraper>.log

set -uo pipefail

SCRAPER="${1:-}"
if [[ -z "$SCRAPER" ]]; then
  echo "[$(date -u +%FT%TZ)] ERROR: scraper name required" >&2
  exit 64
fi

# Load secrets
if [[ ! -r "$HOME/.econdelta.env" ]]; then
  echo "[$(date -u +%FT%TZ)] ERROR: ~/.econdelta.env not found or unreadable" >&2
  exit 64
fi
# shellcheck disable=SC1091
source "$HOME/.econdelta.env"

: "${ECONDELTA_HOME:?ECONDELTA_HOME not set}"
: "${ECONDELTA_VPS:?ECONDELTA_VPS not set}"
: "${ECONDELTA_VPS_REPO:?ECONDELTA_VPS_REPO not set}"

cd "$ECONDELTA_HOME"

LOG_FILE="$ECONDELTA_HOME/logs/launchd-${SCRAPER}.log"
mkdir -p "$(dirname "$LOG_FILE")"

{
  echo ""
  echo "=== $(date -u +%FT%TZ) run-and-sync.sh $SCRAPER ==="

  # Activate venv (use python directly since shebang paths may be stale)
  VENV_PYTHON="$ECONDELTA_HOME/.venv/bin/python"
  if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "ERROR: venv python not found at $VENV_PYTHON"
    exit 1
  fi

  # Run scraper
  echo "+ $VENV_PYTHON -m scrapers.$SCRAPER"
  "$VENV_PYTHON" -m "scrapers.$SCRAPER"
  RC=$?

  echo "scraper exit code: $RC"

  # Rsync on RC 0 (clean) or RC 2 (anomaly hold) — bb_forex still writes a
  # snapshot on hold (fresh rates + held reserves; see scrapers/bb_forex.py
  # main()), so skipping the sync on RC 2 would strand that write on the
  # laptop forever. A scraper that skips the write entirely on RC 2 just
  # gets a harmless no-op rsync.
  if [[ "$RC" -eq 0 || "$RC" -eq 2 ]]; then
    if [[ "$RC" -eq 2 ]]; then
      echo "INFO: anomaly hold — snapshot written, syncing"
    fi
    SRC_DIR="$ECONDELTA_HOME/data/$SCRAPER"
    DEST="$ECONDELTA_VPS:$ECONDELTA_VPS_REPO/data/$SCRAPER/"
    echo "+ rsync $SRC_DIR/ -> $DEST"
    rsync -az --delete-after "$SRC_DIR/" "$DEST"
    RSYNC_RC=$?
    echo "rsync exit code: $RSYNC_RC"
    if [[ "$RSYNC_RC" -ne 0 ]]; then
      echo "WARN: rsync failed; laptop snapshot exists but VPS not updated"
      exit 3
    fi
    # Trigger VPS aggregator so latest.json refreshes immediately
    echo "+ ssh remote aggregator trigger"
    ssh -o ConnectTimeout=10 "$ECONDELTA_VPS" "sudo systemctl start econdelta-aggregate.service" 2>&1 || echo "WARN: remote aggregator trigger failed (non-fatal)"
  else
    echo "ERROR: scraper failed (exit $RC); Discord alert fired by scraper"
  fi

  exit "$RC"
} >> "$LOG_FILE" 2>&1
