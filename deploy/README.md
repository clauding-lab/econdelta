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

## Coordinated release order (data-reliability repair, R2 + R3)

**TLDR.** EconDelta ships first; The Brief ships last; a rollback starts with The Brief. The
full step list, with the pause and the Brief-side commands, is in The Brief's runbook
(`the-brief` repo, `deploy/README.md`, section "Coordinated release order"). The older
"Brief consumer first" order (R2 fix 12) is superseded: it was derived from a never-deployed
hypothetical and reversed once measured on the deployed Brief (controller ruling 28 Sep 2026,
step 2 amended 29 Sep 2026; AGENTS.md landmine 60).

1. **This producer first, including the H5 NBR dating fix.** Merge `econdelta#137` on the
   release night before the 01:00 BDT `econdelta-gitpull` (or earlier). The pull takes `main`
   only; the branch changes no unit, so no `install.sh` run is needed.
2. **History repair R1 with exclusions (d) + (l), owner-applied**, after that merge (optional)
   but BEFORE the new producer's first `metric_history` write: the 02:55 BDT aggregate, the
   03:15 BDT retry, a catch-up fire on an install, or any manual run. If R1 cannot finish in
   time, stop `econdelta-aggregate.timer` and `econdelta-aggregate-retry.timer` until it has
   (landmine 5: starting a `Persistent=true` timer after a missed slot fires it at once).
   Night-1 sequence, every overlapping writer paused (landmine 60; manifest runbook "Safety
   contract" item 4): `python -m scripts.export_history --repair-snapshot DIR --r1-final-backup`
   (a NEW `DIR`); then `python -m scripts.repair_refresh_before_images --manifest <reviewed plan
   manifest.json> ... --backup-dir DIR ...` (exit 0, `0 refused`; record its counts and output
   sha256); then `python -m scripts.repair_observation_history --plan --candidate
   <refreshed-candidate.json> ...` (must print `Validated 2016 operations`); then
   `python -m scripts.repair_observation_history --apply <plan>/manifest.json ... --writers-paused`.
   The refresh run replaces the recheck for a refreshed candidate (run against the same `DIR`,
   the recheck always refuses it, exit 2, by design); `--reference-backup` is still never a
   Night-1 pass.
   Before the new Brief's first fire the owner also applies the round-4 NBR exclusion
   (restamps dated after 2026-09-24, value-bound, the 2026-06-30 period row kept), built by
   `scripts/nbr_round4_candidates.py`; Day-2 sequence in the manifest runbook
   (`docs/reviews/2026-09-25-history-repair-manifest.md`, "NBR release order" item 4).
3. **The Brief's migration 0006**, owner-applied through the Supabase SQL editor.
4. **The new Brief, before its 08:00 BDT fire**, once this producer's first daily write has
   landed (the Hetzner `latest.json` carries a `write_status` block); steps in `the-brief`
   `deploy/README.md`.

**Transition window** (The Brief's G2 §8, measured): while the deployed Brief `a810fcb` still
runs on this producer, the new EPB exports rows hide its Trade Gap card while exports lead
imports, and an Opus-review quarantine before the first new-format 02:55 BDT archive drops the
rejected family (no carried-over number), so that issue shows no USD/BDT, gold, Brent or WTI
value and a false DSE "Non-trading day" reason. Watch the first night after the merge. The
full list, and what to do if a later step slips, is in The Brief's runbook.

**Roll back the Brief first**, then this producer only if needed: the new Brief on the old
producer drops the USD/BDT, gold, Brent and WTI cards.

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

**Signature:** `systemctl --failed` (or `journalctl -u <unit>`) shows
`status=2` or `status=3` and "Start request repeated too quickly" / the unit
stuck in `failed`, while `logs/<name>-systemd.log` and the corresponding
`run_logs` row (Supabase) show a clean, by-design outcome — e.g.
`econdelta-npl-structure.service` logging "FSR position ... already captured
— skip" (`run_logs.status = 'skip'`), or `econdelta-dse.service` /
`econdelta-commodity.service` logging an anomaly-vs-previous-day hold
(`run_logs.status = 'stale'`).

**Cause:** several scrapers return a non-zero exit code for a condition that
is a deliberate no-op, not a failure — see
`utils/supabase_writer._STATUS_BY_EXIT` (`0` ok, `1` fail, `2` stale, `3`
skip). Without telling systemd about it, `Restart=on-failure` treats that
exit as a real failure: it restart-loops (re-running the same fetch/scrape
each time, and a deterministic anomaly or an already-captured report
reaches the exact same verdict every retry) until `StartLimitBurst` trips,
and the unit is left in `failed` state — which pollutes exactly the
`systemctl --failed` signal used to verify box health, and duplicates the
Discord alert once per retry.

**Fix:** the unit declares either `SuccessExitStatus=<code>` (systemd treats
the exit as fully clean — `inactive`, not `failed`, and `Restart=on-failure`
never fires for it) or `RestartPreventExitStatus=<code>` (stops the retry
loop but, unlike `SuccessExitStatus`, does **not** keep the unit out of
`failed` state — see AGENTS.md landmine 48 for why these are not
interchangeable) for its documented non-error exit code:

| Unit | Exit code | Meaning | Directive |
|---|---|---|---|
| `econdelta-npl-structure.service` | 3 (skip) | FSR position already captured | `SuccessExitStatus=3` |
| `econdelta-forex.service` | 2 (stale) | rate/reserves anomaly, write held | `RestartPreventExitStatus=2` |
| `econdelta-briefing.service` | 2 (stale) | data-freshness gate skip | `RestartPreventExitStatus=2` |
| `econdelta-dse.service` | 2 (stale) | index/market anomaly, write skipped | `RestartPreventExitStatus=2` |
| `econdelta-commodity.service` | 2 (stale) | price anomaly, write skipped | `RestartPreventExitStatus=2` |

Audited across every scraper and the parse/aggregate orchestrators
(`grep -n "return 3" scrapers/*.py parse_all.py aggregate_latest.py`),
`econdelta-npl-structure.service` is the only unit with a documented exit-3
skip. The four exit-2 rows are not a fleet-wide-deliberate design from the
start — `econdelta-dse.service` and `econdelta-commodity.service` had no
exit-2 handling at all until this was caught in review, exactly the same
storming failure mode `econdelta-npl-structure.service` hit for its exit-3
case. Whether "stale" should eventually read fully green
(`SuccessExitStatus=2`) instead of failed-but-not-storming is an owner-level
monitoring-semantics call, deliberately left open for now.

**Verify:** `tests/test_deploy_units.py::test_npl_structure_treats_documented_skip_exit_as_success`
and `test_documented_exit_2_units_do_not_retry_storm` (with its
`test_exit_2_source_files_still_document_a_stale_exit` sanity check) pin
this for all five units above.

**Rollout note:** `daemon-reload` does **not** clear a unit's existing
`failed` state — after copying a fixed unit file onto the box, also run
`sudo systemctl reset-failed <unit>.service` for any unit currently showing
failed, or it stays red in `systemctl --failed` until its next scheduled
fire quietly clears it.

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
open file descriptor). The file is world-readable (`0644`) either way, so
this is purely a **write-side** problem: `/etc/logrotate.d/econdelta` runs
`su adnan-local adnan-local` (required because the parent `logs/` directory
is user-owned) with `copytruncate`, which opens the file `O_RDWR` to
truncate it in place after copying — a non-root `adnan-local` can read a
644 root-owned file fine, but cannot open it for writing, so that step
fails and the whole logrotate run for econdelta exits non-zero.

The trap only **arms** when the log file is genuinely absent —
`copytruncate` never removes a log, and `deploy/uninstall.sh` preserves
`logs/` across a reinstall, so an already-running unit's existing log
doesn't spontaneously flip into this state on an ordinary restart. The case
that actually matters is a **newly added unit**: it starts with no log file
at all, so it's born with the trap already armed. That's exactly what the
regression test below checks on every unit, present and future.

**Fix:** every unit that writes to a persistent log carries a
`+`-privileged `ExecStartPre` that refuses a symlinked target, then touches
and re-chowns its own log file, immediately before `ExecStart` runs:

```
ExecStartPre=+/bin/sh -c '[ -L <path> ] && exit 1; touch <path> && chmod 0644 <path> && chown <User>:<Group> <path>'
```

The leading `+` runs this specific command with full privileges and
**outside** the unit's own sandboxing (`User=`, `Group=`, `ProtectHome=`,
`ProtectSystem=`) regardless of how those are set for the main process —
the standard systemd idiom for a privileged setup step ahead of an
unprivileged `ExecStart`. `[ -L <path> ] && exit 1` aborts the whole start
rather than chowning/chmoding through a swapped symlink (CWE-59). `touch`
never truncates an existing file, so a log that already has real content is
untouched; `chown` then fixes ownership — to this unit's own `User=`/`Group=`,
not a hardcoded name — no matter who most recently (re)created the file;
`chmod 0644` is deliberately this guard's mode authority for the log file,
not just a touch side effect. This closes the trap at its exact source, on
every start, with no new unit, no new timer, and no race window (unlike a
periodic sweep, which only chowns on its own schedule and can miss a
restart that lands after the last sweep but before the next logrotate run).

**Verify:** `tests/test_deploy_units.py::test_every_appended_log_has_a_privileged_ownership_guard`
enforces every `StandardOutput=`/`StandardError=` `append:`/`file:` target
has a matching guard chowning to that exact unit's own `User=`/`Group=`, so
a future new unit (or an edit that drops this line, or one that chowns to
the wrong owner) fails CI instead of surfacing as a 2 a.m. logrotate error.

## Notes

- Scripts are invoked via `sudo bash deploy/install.sh` and do not require the executable bit.
  After cloning, run `chmod +x deploy/*.sh` if you prefer calling them directly.
- `/etc/econdelta.env` is owned `root:adnan-local` mode `0640`. The service user reads it at runtime.
- Logs and data directories are preserved across uninstall runs. To fully reset, remove them manually.
- Units that shell out to the `claude` CLI carry a `*.service.d/10-claude-json-writable.conf` drop-in adding `~/.claude.json` to `ReadWritePaths` — required because the CLI writes that state file each run while the services run under `ProtectHome=read-only` (see `AGENT_LEARNINGS.md`, 2026-05-29).
- Every unit that writes stdout/stderr to a log file (`StandardOutput=append:.../logs/<name>-systemd.log`, or `file:`) carries a matching `+`-privileged `ExecStartPre` that refuses a symlinked target and otherwise touches + chowns that exact file to the unit's own `User=`/`Group=` before `ExecStart` runs — see Troubleshooting above. Adding a new unit with the same pattern must copy this line too (with its own owner and log path), or `tests/test_deploy_units.py::test_every_appended_log_has_a_privileged_ownership_guard` fails.
- `deploy/gitpull.sh` posts a Discord warning ("unit files changed — run install.sh") whenever a pull touches `deploy/*.service`/`*.timer`/`install.sh`. The recommended response for a units-only PR like a `SuccessExitStatus=`/`RestartPreventExitStatus=`/`ExecStartPre=` change (landmine 37) is still a targeted copy of the changed `.service` files + `daemon-reload` — but running the full `sudo bash deploy/install.sh` is also safe whenever the PR adds no new unit: the hardcoded enable-loop (landmine 19) is unchanged, and `systemctl enable --now` on a timer that is already enabled and active is a no-op (no catch-up fire, landmine 5 doesn't apply). Remember `daemon-reload` does not clear an existing `failed` state on its own — see the `reset-failed` note in Troubleshooting above.
