#!/usr/bin/env bash
# EconDelta — systemd installer
# Run as: sudo bash deploy/install.sh
# Idempotent: safe to re-run.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/home/adnan-local/econdelta}"
ENV_FILE="/etc/econdelta.env"
SERVICE_USER="${SERVICE_USER:-adnan-local}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (sudo)." >&2
  exit 1
fi

if [[ ! -d "$REPO_ROOT" ]]; then
  echo "ERROR: repo not found at $REPO_ROOT. Clone first." >&2
  exit 1
fi

if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
  echo "ERROR: venv not found at $REPO_ROOT/.venv — create it first:" >&2
  echo "       python3 -m venv $REPO_ROOT/.venv   # requires Python 3.11+" >&2
  echo "       source $REPO_ROOT/.venv/bin/activate && pip install -r requirements.txt" >&2
  echo "       $REPO_ROOT/.venv/bin/python -m playwright install chromium" >&2
  exit 1
fi

# Version check: Python must be 3.11+
PY_VERSION=$("$REPO_ROOT/.venv/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 11 ]]; }; then
  echo "ERROR: venv Python is $PY_VERSION — requires 3.11+." >&2
  exit 1
fi

# Create env file if missing (user must edit after). Keys mirror the deployed
# /etc/econdelta.env: the scrapers need Supabase write creds, and parse/aggregate
# need a Claude Max token (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`) so
# the CLI works headless under systemd. Leave values blank here; fill before enabling.
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<EOF
# EconDelta environment — edit before enabling timers
DISCORD_WEBHOOK_URL=
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
CLAUDE_CODE_OAUTH_TOKEN=
ECONDELTA_HOME=$REPO_ROOT
ECONDELTA_DRY_RUN=0
EOF
  chmod 0640 "$ENV_FILE"
  chown "root:$SERVICE_USER" "$ENV_FILE"
  echo "NOTE: created $ENV_FILE — edit it with your Discord webhook, Supabase creds, and Claude token."
fi

# Ensure log and data dirs exist with correct ownership
mkdir -p "$REPO_ROOT/logs" "$REPO_ROOT/data"
chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_ROOT/logs" "$REPO_ROOT/data"

# Copy unit files
install -m 0644 "$REPO_ROOT/deploy/"econdelta-*.service /etc/systemd/system/
install -m 0644 "$REPO_ROOT/deploy/"econdelta-*.timer /etc/systemd/system/

# Copy systemd drop-in overrides (e.g. the ~/.claude.json ReadWritePaths carve-out
# that lets the claude CLI write its state file under ProtectHome=read-only — see
# AGENT_LEARNINGS 2026-05-29). install(1) won't create the .d dir, so mkdir first.
for dropin_dir in "$REPO_ROOT/deploy/"econdelta-*.service.d; do
  [[ -d "$dropin_dir" ]] || continue
  unit_name="$(basename "$dropin_dir")"
  mkdir -p "/etc/systemd/system/$unit_name"
  install -m 0644 "$dropin_dir"/*.conf "/etc/systemd/system/$unit_name/"
done

# Logrotate
install -m 0644 "$REPO_ROOT/deploy/logrotate.conf" /etc/logrotate.d/econdelta

systemctl daemon-reload

# Enable timers (not services — services are triggered by timers). Includes the
# retry timers (forex/aggregate/parse) that backstop the primary daily fires.
for t in econdelta-forex econdelta-commodity econdelta-aggregate econdelta-dse econdelta-fetch econdelta-parse \
         econdelta-forex-retry econdelta-aggregate-retry econdelta-parse-retry econdelta-briefing; do
  systemctl enable --now "${t}.timer"
done

echo ""
echo "Installed. Next steps:"
echo "  1. Edit $ENV_FILE and set DISCORD_WEBHOOK_URL / Supabase creds / Claude token"
echo "  2. Verify: systemctl list-timers | grep econdelta"
echo "  3. Manual test: sudo systemctl start econdelta-forex.service"
echo "  4. Tail logs: journalctl -u econdelta-forex.service -f"
