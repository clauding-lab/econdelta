# EconDelta — VPS Deployment

## First-time install

```bash
# On VPS, as adnan-local:
cd ~
git clone git@github.com:clauding-lab/econdelta.git
cd econdelta
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

# As root:
sudo bash deploy/install.sh
sudo vim /etc/econdelta.env   # set DISCORD_WEBHOOK_URL, MEDIA_SCREEN_WEBHOOK_URL (#thebrief), Supabase creds, CLAUDE_CODE_OAUTH_TOKEN
                               # (leave ECONDELTA_PROVENANCE_ENABLED at its 0 default here — only
                               #  flip to 1 after supabase/migrations/0013_provenance.sql is applied,
                               #  see AGENTS.md landmine 43)
```

## Verify

```bash
systemctl list-timers | grep econdelta
# All 9 timers (6 primary + 3 retry) should show with next-run time in UTC.
```

## Manual run

```bash
sudo systemctl start econdelta-forex.service
journalctl -u econdelta-forex.service -n 50
cat data/bb_forex/$(date -u +%F).json
```

## Update deployment

```bash
cd ~/econdelta
git pull
source .venv/bin/activate && pip install -r requirements.txt   # if requirements changed
sudo bash deploy/install.sh   # re-install units if any changed (incl. service .d/ drop-ins)
```

## Rollback

```bash
sudo bash deploy/uninstall.sh
```

## Schedule (UTC — Bangladesh = UTC+6)

| Timer | UTC | BDT |
|---|---|---|
| econdelta-gitpull         | 19:00 | 01:00 (+1) |
| econdelta-fetch           | 19:10 | 01:10 (+1) |
| econdelta-forex           | 19:15 | 01:15 (+1) |
| econdelta-commodity       | 19:18 | 01:18 (+1) |
| econdelta-dse             | 19:21 | 01:21 (+1) |
| econdelta-auction         | 19:24 | 01:24 (+1) |
| econdelta-pink-sheet      | 19:27 | 01:27 (+1) |
| econdelta-dse-dayend      | 19:30 | 01:30 (+1) |
| econdelta-imf-eff         | Sun 19:33 | Mon 01:33 |
| econdelta-imf-debt        | Sun 19:36 | Mon 01:36 |
| econdelta-fiscal-gdp      | Sun 19:39 | Mon 01:39 |
| econdelta-forex-retry     | 19:50 | 01:50 (+1) |
| econdelta-parse           | 20:10 | 02:10 (+1) |
| econdelta-parse-retry     | 20:35 | 02:35 (+1) |
| econdelta-aggregate       | 20:55 | 02:55 (+1) |
| econdelta-aggregate-retry | 21:15 | 03:15 (+1) |
| econdelta-sentinel        | 21:35 | 03:35 (+1) |
| econdelta-npl-structure   | Sun 23:29 | Mon 05:29 |
| econdelta-briefing        | Mon 01:00 | Mon 07:00 |
| econdelta-media-screen    | 15:30 | 21:30 |

Pipeline order: fetch → forex/commodity/dse/auction/pink-sheet scrapers → parse (deterministic + Claude hybrid) → aggregate (writes `data/latest.json` + Supabase `metric_history`). The daily aggregate (including its retry) completes by ~21:15 UTC (03:15 BDT).

**Two constraints fix these times — do not move them casually.**

1. **Downstream.** The Brief publishes at **08:00 BDT** and reads what this chain wrote. The aggregate landing at 02:55 BDT gives it ~5 h of headroom. Until 2026-08-04 the aggregate ran at 13:00 BDT while The Brief fired at 06:30, so every brief was built on an aggregate **~17 h old** and a fix landing here in the morning could not reach the next morning's issue. If either side moves, re-check the other.
2. **Upstream.** `parse` calls the Claude CLI. It must stay clear of **05:00–06:00 BDT** — that is 16:00–17:00 US Pacific, Anthropic's peak, where the preflight failed 12 consecutive times over two days in May 2026 (commit `28bcb3d`). 02:10 BDT is 13:10 Pacific, comfortably outside it. This is the binding limit on how late the chain can start.

## Restricted SSH keys

Two keys let Copotron (the Discord agent on the Hetzner box) act here without a
shell. Both are forced commands: the `command="…"` prefix in `authorized_keys`
replaces whatever the client asks for, and the request lands in
`SSH_ORIGINAL_COMMAND` for the wrapper to accept or refuse.

| Key | Wrapper | May run |
|---|---|---|
| `copotron-media-decide` | `deploy/media-decide-ssh.sh` | `approve <id>` / `reject <id>` |
| `copotron-ops` | `deploy/copotron-ops-ssh.sh` | `pull`, `head`, `timers`, `status <unit>`, `log <unit> [n]`, `logs`, `help` |

The ops key exists because Copotron can read the Supabase tables but had no view
of the *box* — timer state, unit health, error logs — and twice reported an
inference about the box as fact. It is read-mostly: the only write is
`git pull --ff-only origin main` via `gitpull.sh`, which keeps its own branch
guard.

Unlike the media-decide wrapper, **the ops wrapper never sources
`/etc/econdelta.env`** — the Supabase service-role key and the Claude OAuth
token stay out of its environment, and log output is scrubbed of key-shaped
strings on the way out. The cost of that: a manual `ssh exonhost pull` writes no
`run_logs` row and posts no Discord alert (both are best-effort in `gitpull.sh`),
so it degrades to a plain guarded pull. The nightly `econdelta-gitpull.timer`
run is the logged one. If a future verb genuinely needs those secrets, it belongs
behind a different key — do not add the env file to this one.

Install (as `adnan-local`, one line, `~/.ssh/authorized_keys`):

```
command="/home/adnan-local/econdelta/deploy/copotron-ops-ssh.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 <PUBKEY> copotron-ops
```

`chmod +x deploy/copotron-ops-ssh.sh` after cloning if the executable bit did not
survive. Dispatch and refusal behaviour is covered by
`tests/test_copotron_ops_ssh.py`.

## Troubleshooting

### A unit shows up in `systemctl --failed` but its own log says it worked as designed

**Signature:** `systemctl --failed` (or `journalctl -u <unit>`) shows `status=3`
and "Start request repeated too quickly" / the unit stuck in `failed`, while
`logs/<name>-systemd.log` and the corresponding `run_logs` row (Supabase)
show a clean, by-design skip — e.g. `econdelta-npl-structure.service` logging
"FSR position ... already captured — skip" (`run_logs.status = 'skip'`).

**Cause:** a handful of scrapers return a non-zero exit code for a condition
that is a deliberate no-op, not a failure — see
`utils/supabase_writer._STATUS_BY_EXIT` (`0` ok, `1` fail, `2` stale, `3`
skip). Without telling systemd about it, `Restart=on-failure` treats that
exit as a real failure: it restart-loops (re-running the full extraction
attempt each time) until `StartLimitBurst` trips, and the unit is left in
`failed` state — which pollutes exactly the `systemctl --failed` signal used
to verify box health.

**Fix:** the unit declares `SuccessExitStatus=<code>` for its documented
no-op exit code, so systemd treats it as a clean exit (`inactive`, not
`failed`) and `Restart=on-failure` never fires for it. Only
`econdelta-npl-structure.service` currently has a documented **skip** (exit
3) — audited across every scraper and the parse/aggregate orchestrators
(`grep -n "return 3" scrapers/*.py parse_all.py aggregate_latest.py`), it is
the only `main()` that returns 3. `bb_forex.service` and
`briefing.service` separately use `RestartPreventExitStatus=2` for their
documented **stale** exit (2) — that stops the retry loop but, unlike
`SuccessExitStatus`, does **not** keep the unit out of `failed` state (see
AGENTS.md landmine 47); those two are a pre-existing, differently-scoped
design and were left untouched by this fix.

**Verify:** `tests/test_deploy_units.py::test_npl_structure_treats_documented_skip_exit_as_success`
pins this for the npl-structure unit.

### Nightly logrotate fails: `error opening ".../logs/<name>-systemd.log": Permission denied`

**Signature:** `/etc/logrotate.d/econdelta` fails around midnight with a
permission-denied error opening one specific `*-systemd.log` file; every
other econdelta log rotates fine that night (logrotate keeps processing the
rest of the glob after one file errors, but exits non-zero overall).

**Cause:** every unit writes its log via `StandardOutput=append:<path>` /
`StandardError=append:<path>` running as `User=adnan-local`. If that log
file is **absent** when the unit (re)starts, systemd creates the append
target itself, as **root**, before `ExecStart` drops privileges to
`User=`/`Group=` — the service still writes to it fine (it inherited the
open file descriptor), but the file is now owned `root:root` on disk.
`/etc/logrotate.d/econdelta` runs `su adnan-local adnan-local` (required
because the parent `logs/` directory is user-owned) with `copytruncate`,
which needs to open the file for reading *and* truncate it — a non-root
`adnan-local` cannot do either to a root-owned file. A unit restart late at
night (crash, manual `systemctl restart`, a fresh log after `deploy/uninstall.sh`
+ reinstall, etc.) is enough to reintroduce the trap before the next
midnight run.

**Fix:** every unit that appends to a persistent log carries a
`+`-privileged `ExecStartPre` that touches and re-chowns its own log file
immediately before `ExecStart` runs:

```
ExecStartPre=+/bin/sh -c 'touch <path> && chmod 0644 <path> && chown adnan-local:adnan-local <path>'
```

The leading `+` runs this specific command with full privileges and
**outside** the unit's own sandboxing (`User=`, `Group=`, `ProtectHome=`,
`ProtectSystem=`) regardless of how those are set for the main process —
the standard systemd idiom for a privileged setup step ahead of an
unprivileged `ExecStart`. `touch` never truncates an existing file, so a
log that already has real content is untouched; `chown` then fixes
ownership no matter who most recently (re)created the file. This closes
the trap at its exact source, on every start, with no new unit, no new
timer, and no race window (unlike a periodic sweep, which only chowns on
its own schedule and can miss a restart that lands after the last sweep
but before the next logrotate run).

**Verify:** `tests/test_deploy_units.py::test_every_appended_log_has_a_privileged_ownership_guard`
enforces every `StandardOutput=append:` target has a matching guard, so a
future new unit (or an edit that drops this line) fails CI instead of
surfacing as a 2 a.m. logrotate error.

## Notes

- Scripts are invoked via `sudo bash deploy/install.sh` and do not require the executable bit.
  After cloning, run `chmod +x deploy/*.sh` if you prefer calling them directly.
- `/etc/econdelta.env` is owned `root:adnan-local` mode `0640`. The service user reads it at runtime.
- Logs and data directories are preserved across uninstall runs. To fully reset, remove them manually.
- Units that shell out to the `claude` CLI carry a `*.service.d/10-claude-json-writable.conf` drop-in adding `~/.claude.json` to `ReadWritePaths` — required because the CLI writes that state file each run while the services run under `ProtectHome=read-only` (see `AGENT_LEARNINGS.md`, 2026-05-29).
- Every unit that appends stdout/stderr to a log file (`StandardOutput=append:.../logs/<name>-systemd.log`) carries a matching `+`-privileged `ExecStartPre` that touches and chowns that exact file to `adnan-local:adnan-local` before `ExecStart` runs — see Troubleshooting above. Adding a new unit with the same append pattern must copy this line too, or `tests/test_deploy_units.py::test_every_appended_log_has_a_privileged_ownership_guard` fails.
