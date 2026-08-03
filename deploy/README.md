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
| econdelta-fetch           | 23:00 | 05:00 (+1) |
| econdelta-forex           | 23:05 | 05:05 (+1) |
| econdelta-commodity       | 23:08 | 05:08 (+1) |
| econdelta-dse             | 23:11 | 05:11 (+1) |
| econdelta-forex-retry     | 00:00 | 06:00 |
| econdelta-parse           | 04:30 | 10:30 |
| econdelta-parse-retry     | 05:55 | 11:55 |
| econdelta-aggregate       | 07:00 | 13:00 |
| econdelta-aggregate-retry | 08:00 | 14:00 |

Pipeline order: fetch → forex/commodity/dse scrapers → parse (deterministic + Claude hybrid) → aggregate (writes `data/latest.json` + Supabase `metric_history`). The daily aggregate (including its retry) completes by ~08:00 UTC (14:00 BDT); The Brief reads the published data after that.

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

## Notes

- Scripts are invoked via `sudo bash deploy/install.sh` and do not require the executable bit.
  After cloning, run `chmod +x deploy/*.sh` if you prefer calling them directly.
- `/etc/econdelta.env` is owned `root:adnan-local` mode `0640`. The service user reads it at runtime.
- Logs and data directories are preserved across uninstall runs. To fully reset, remove them manually.
- The parse + aggregate services carry a `*.service.d/10-claude-json-writable.conf` drop-in adding `~/.claude.json` to `ReadWritePaths` — required because the `claude` CLI writes that state file each run while the services run under `ProtectHome=read-only` (see `AGENT_LEARNINGS.md`, 2026-05-29).
