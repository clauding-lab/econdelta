#!/usr/bin/env bash
# EconDelta — systemd installer
# Run as: sudo bash deploy/install.sh
# Idempotent: safe to re-run.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/home/adnan/econdelta}"
ENV_FILE="/etc/econdelta.env"
SERVICE_USER="${SERVICE_USER:-adnan}"

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

# Create env file if missing (user must edit after)
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<EOF
# EconDelta environment — edit before enabling timers
DISCORD_WEBHOOK_URL=
ECONDELTA_HOME=$REPO_ROOT
ECONDELTA_DRY_RUN=0
EOF
  chmod 0640 "$ENV_FILE"
  chown "root:$SERVICE_USER" "$ENV_FILE"
  echo "NOTE: created $ENV_FILE — edit it with your Discord webhook URL."
fi

# Ensure log and data dirs exist with correct ownership
mkdir -p "$REPO_ROOT/logs" "$REPO_ROOT/data"
chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_ROOT/logs" "$REPO_ROOT/data"

# Copy unit files
install -m 0644 "$REPO_ROOT/deploy/"econdelta-*.service /etc/systemd/system/
install -m 0644 "$REPO_ROOT/deploy/"econdelta-*.timer /etc/systemd/system/

# Logrotate
install -m 0644 "$REPO_ROOT/deploy/logrotate.conf" /etc/logrotate.d/econdelta

systemctl daemon-reload

# Enable timers (not services — services are triggered by timers)
for t in econdelta-forex econdelta-commodity econdelta-aggregate econdelta-dse econdelta-fetch econdelta-parse; do
  systemctl enable --now "${t}.timer"
done

echo ""
echo "Installed. Next steps:"
echo "  1. Edit $ENV_FILE and set DISCORD_WEBHOOK_URL"
echo "  2. Verify: systemctl list-timers | grep econdelta"
echo "  3. Manual test: sudo systemctl start econdelta-forex.service"
echo "  4. Tail logs: journalctl -u econdelta-forex.service -f"
