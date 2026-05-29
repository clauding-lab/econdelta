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
sudo vim /etc/econdelta.env   # set DISCORD_WEBHOOK_URL, Supabase creds, CLAUDE_CODE_OAUTH_TOKEN
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

## Notes

- Scripts are invoked via `sudo bash deploy/install.sh` and do not require the executable bit.
  After cloning, run `chmod +x deploy/*.sh` if you prefer calling them directly.
- `/etc/econdelta.env` is owned `root:adnan-local` mode `0640`. The service user reads it at runtime.
- Logs and data directories are preserved across uninstall runs. To fully reset, remove them manually.
- The parse + aggregate services carry a `*.service.d/10-claude-json-writable.conf` drop-in adding `~/.claude.json` to `ReadWritePaths` — required because the `claude` CLI writes that state file each run while the services run under `ProtectHome=read-only` (see `AGENT_LEARNINGS.md`, 2026-05-29).
