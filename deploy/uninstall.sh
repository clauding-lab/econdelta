#!/usr/bin/env bash
# EconDelta — systemd uninstaller
# Run as: sudo bash deploy/uninstall.sh
# Disables and removes all econdelta systemd units. Preserves logs + data.

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (sudo)." >&2
  exit 1
fi

for t in econdelta-forex econdelta-commodity econdelta-aggregate econdelta-dse econdelta-fetch econdelta-parse; do
  systemctl disable --now "${t}.timer" 2>/dev/null || true
done

rm -f /etc/systemd/system/econdelta-*.service
rm -f /etc/systemd/system/econdelta-*.timer
rm -f /etc/logrotate.d/econdelta

systemctl daemon-reload

echo "Uninstalled. Preserved: /etc/econdelta.env, logs/, data/"
echo "To fully clean: sudo rm /etc/econdelta.env"
