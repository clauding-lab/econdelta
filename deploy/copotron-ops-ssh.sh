#!/usr/bin/env bash
# Forced-command wrapper for Copotron's read-mostly ops SSH key (ExonVPS).
#
# Sibling of deploy/media-decide-ssh.sh, same shape: the restricted
# authorized_keys entry pins this script as the ONLY command the key may run,
# and the caller's request arrives in SSH_ORIGINAL_COMMAND. Anything not in the
# verb table below is refused before it can reach a shell.
#
# WHY THIS EXISTS. Copotron (the Discord agent on the Hetzner box) could read
# EconDelta's Supabase tables but had no view of the BOX — whether a timer was
# stuck, what an error log actually said, whether a unit had died. It inferred
# that from source code and DB side-effects instead, and got it wrong twice in
# one week: it reported "46 stale metrics" as broken fetchers when 40 were a
# writer-less archive table, and it told the owner a manual `git pull` was
# needed on 2026-08-03 when econdelta-gitpull.timer had been auto-pulling all
# along. Both were guesses dressed as facts. This key closes that gap.
#
# WHAT IT DELIBERATELY CANNOT DO. No shell, no arbitrary command, no writes
# outside `git pull --ff-only origin main`, and — unlike media-decide-ssh.sh —
# it NEVER sources /etc/econdelta.env. The Supabase service-role key and the
# Claude OAuth token stay out of this script's environment entirely. That is a
# promise made to the owner when he granted the key, so keep it: if a future
# verb seems to need those secrets, it belongs in a different key, not here.
# Log output is scrubbed of key-shaped strings on the way out (see _scrub) so
# that a secret accidentally logged by some other service cannot be read back
# through this channel either.
#
# Verbs:
#   pull                 git pull --ff-only origin main, via deploy/gitpull.sh
#   head                 current branch + HEAD sha/subject + porcelain status
#   timers               systemctl list-timers 'econdelta-*'
#   status <unit>        systemctl status econdelta-<unit>.service
#   log <unit> [n]       last n (<=200, default 60) lines of logs/<unit>-systemd.log
#   logs                 list the log files that exist, with size + mtime
#   help                 print this verb table
#
# <unit> is [a-z0-9_-] only — no dots, no slashes — so neither the systemd unit
# name nor the log path can be escaped into something else. Underscores are in
# the charset because the two namespaces do NOT agree: units are hyphenated
# (econdelta-media-screen.service) while the log files those units write are
# snake_case (logs/media_screen-systemd.log, bb_forex, world_bank_pink_sheet).
# Half the logs are unreachable without them. See tests/test_copotron_ops_ssh.py,
# which derives both name sets from deploy/*.service so a rename cannot silently
# put a log out of reach.
#
# authorized_keys entry (one line):
#   command="/home/adnan-local/econdelta/deploy/copotron-ops-ssh.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 <PUBKEY> copotron-ops
#
# COPOTRON_OPS_DRYRUN=1 prints the dispatch decision instead of acting (used by
# tests/test_copotron_ops_ssh.py). It can only ever make this script do LESS,
# and sshd does not pass client environment by default, so it is not a bypass.
set -uo pipefail

REPO="${ECONDELTA_HOME:-/home/adnan-local/econdelta}"
UNIT_RE='^[a-z0-9][a-z0-9_-]{0,40}$'
MAX_LOG_LINES=200
DEFAULT_LOG_LINES=60

usage() {
  sed -n '/^# Verbs:/,/^#$/p' "$0" | sed 's/^# \{0,1\}//'
}

refuse() {
  echo "refused: $1" >&2
  echo "this key accepts only: pull | head | timers | status <unit> | log <unit> [n] | logs | help" >&2
  exit 2
}

# Redact key-shaped strings from anything we echo back. Belt-and-braces: this
# script never loads secrets itself, but other services write to these logs.
_scrub() {
  sed -E \
    -e 's/(sb_[A-Za-z0-9]*_[A-Za-z0-9_-]{8,})/[REDACTED-supabase-key]/g' \
    -e 's/(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})/[REDACTED-jwt]/g' \
    -e 's/(sk-ant-[A-Za-z0-9_-]{10,})/[REDACTED-anthropic-key]/g' \
    -e 's/(https:\/\/[a-z0-9]+\.supabase\.co[^ "]*(apikey|access_token)=)[^ "&]+/\1[REDACTED]/g'
}

dry() { [[ -n "${COPOTRON_OPS_DRYRUN:-}" ]]; }

cmd="${SSH_ORIGINAL_COMMAND:-}"
# Collapse surrounding whitespace, then split on single spaces only. No eval,
# no word-splitting of the raw string through a shell.
cmd="${cmd#"${cmd%%[![:space:]]*}"}"
cmd="${cmd%"${cmd##*[![:space:]]}"}"
[[ "$cmd" == *$'\n'* || "$cmd" == *$'\r'* ]] && refuse "newline in command"

read -r -a argv <<< "$cmd"
verb="${argv[0]:-help}"

case "$verb" in
  help)
    [[ ${#argv[@]} -le 1 ]] || refuse "'help' takes no arguments"
    usage
    exit 0
    ;;

  pull)
    [[ ${#argv[@]} -eq 1 ]] || refuse "'pull' takes no arguments"
    dry && { echo "DRYRUN pull"; exit 0; }
    cd "$REPO" || { echo "econdelta repo not found at $REPO" >&2; exit 1; }
    # gitpull.sh carries the real guards (refuses unless on main, --ff-only,
    # alerts on unit-file changes). Its run_logs + Discord writes are
    # best-effort and WILL no-op here, because we deliberately do not load
    # /etc/econdelta.env — so an out-of-band pull is unlogged by design. The
    # nightly econdelta-gitpull.timer run is the logged one.
    exec bash deploy/gitpull.sh
    ;;

  head)
    [[ ${#argv[@]} -eq 1 ]] || refuse "'head' takes no arguments"
    dry && { echo "DRYRUN head"; exit 0; }
    cd "$REPO" || { echo "econdelta repo not found at $REPO" >&2; exit 1; }
    echo "repo:   $REPO"
    echo "branch: $(git symbolic-ref --short HEAD 2>/dev/null || echo DETACHED)"
    git log -1 --format='HEAD:   %H%n        %s%n        %ci' 2>/dev/null
    echo "status:"
    git status --porcelain=v1 --untracked-files=no 2>/dev/null | sed 's/^/        /'
    exit 0
    ;;

  timers)
    [[ ${#argv[@]} -eq 1 ]] || refuse "'timers' takes no arguments"
    dry && { echo "DRYRUN timers"; exit 0; }
    systemctl list-timers 'econdelta-*' --all --no-pager
    exit $?
    ;;

  status)
    [[ ${#argv[@]} -eq 2 ]] || refuse "'status' takes exactly one <unit>"
    unit="${argv[1]}"
    [[ "$unit" =~ $UNIT_RE ]] || refuse "bad unit name ${unit@Q} (expected [a-z0-9_-])"
    dry && { echo "DRYRUN status econdelta-$unit.service"; exit 0; }
    systemctl status --no-pager --lines=0 "econdelta-${unit}.service"
    exit $?
    ;;

  logs)
    [[ ${#argv[@]} -eq 1 ]] || refuse "'logs' takes no arguments"
    dry && { echo "DRYRUN logs"; exit 0; }
    cd "$REPO/logs" 2>/dev/null || { echo "no logs dir at $REPO/logs" >&2; exit 1; }
    ls -lh --time-style=long-iso -- *-systemd.log 2>/dev/null || echo "(no *-systemd.log files)"
    exit 0
    ;;

  log)
    [[ ${#argv[@]} -ge 2 && ${#argv[@]} -le 3 ]] || refuse "'log' takes <unit> and an optional line count"
    unit="${argv[1]}"
    [[ "$unit" =~ $UNIT_RE ]] || refuse "bad unit name ${unit@Q} (expected [a-z0-9_-])"
    lines="${argv[2]:-$DEFAULT_LOG_LINES}"
    [[ "$lines" =~ ^[0-9]{1,3}$ ]] || refuse "line count must be a number"
    (( lines >= 1 && lines <= MAX_LOG_LINES )) || refuse "line count must be 1..$MAX_LOG_LINES"
    dry && { echo "DRYRUN log $unit $lines"; exit 0; }
    f="$REPO/logs/${unit}-systemd.log"
    [[ -f "$f" ]] || { echo "no such log: logs/${unit}-systemd.log (try 'logs')" >&2; exit 1; }
    tail -n "$lines" -- "$f" | _scrub
    exit 0
    ;;

  *)
    refuse "unknown verb ${verb@Q}"
    ;;
esac
