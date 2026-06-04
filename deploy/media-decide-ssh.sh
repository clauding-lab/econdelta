#!/usr/bin/env bash
# Forced-command wrapper for Copotron's media-screen approve/reject SSH key.
#
# The restricted authorized_keys entry on this box (ExonVPS) pins this script as
# the ONLY command that key may run, and passes the caller's request via
# SSH_ORIGINAL_COMMAND. We accept EXACTLY "approve <id>" or "reject <id>" (id =
# positive integer) and nothing else — so a key compromise on the Hetzner bot
# box can flip a media_review row's status but cannot run arbitrary commands or
# touch metric_history. The Supabase service-role key never leaves this box.
#
# authorized_keys entry (one line):
#   command="/home/adnan-local/econdelta/deploy/media-decide-ssh.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 <PUBKEY> copotron-media-decide
set -uo pipefail

cmd="${SSH_ORIGINAL_COMMAND:-}"
if [[ ! "$cmd" =~ ^(approve|reject)[[:space:]]+([0-9]+)$ ]]; then
  echo "refused: this key may only run 'approve <id>' or 'reject <id>'" >&2
  exit 2
fi
action="${BASH_REMATCH[1]}"
review_id="${BASH_REMATCH[2]}"

cd /home/adnan-local/econdelta || { echo "econdelta repo not found" >&2; exit 1; }
set -a
# shellcheck disable=SC1091
. /etc/econdelta.env 2>/dev/null
set +a
exec ./.venv/bin/python -m media_screen.decide "$action" "$review_id" --actor "discord:adnan"
