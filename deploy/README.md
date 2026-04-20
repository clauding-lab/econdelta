# EconDelta — VPS Deployment

## First-time install

```bash
# On VPS, as adnan:
cd ~/Projects/clauding-lab
git clone git@github.com:clauding-lab/econdelta.git
cd econdelta
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

# As root:
sudo bash deploy/install.sh
sudo vim /etc/econdelta.env   # paste real DISCORD_WEBHOOK_URL
```

## Verify

```bash
systemctl list-timers | grep econdelta
# All 4 timers should show with next-run time in UTC.
```

## Manual run

```bash
sudo systemctl start econdelta-forex.service
journalctl -u econdelta-forex.service -n 50
cat data/bb_forex/$(date -u +%F).json
```

## Update deployment

```bash
cd ~/Projects/clauding-lab/econdelta
git pull
source .venv/bin/activate && pip install -r requirements.txt   # if requirements changed
sudo bash deploy/install.sh   # re-install units if any changed
```

## Rollback

```bash
sudo bash deploy/uninstall.sh
```

## Schedule (UTC — Bangladesh = UTC+6)

| Service | UTC | BDT |
|---|---|---|
| econdelta-forex      | 00:05 | 06:05 |
| econdelta-commodity  | 00:08 | 06:08 |
| econdelta-aggregate  | 00:20 + 10:35 | 06:20 + 16:35 |
| econdelta-dse        | 10:30 | 16:30 |

The Brief agent runs at 00:30 UTC (06:30 BDT) — pipeline must finish by then.

## Notes

- Scripts are invoked via `sudo bash deploy/install.sh` and do not require the executable bit.
  After cloning, run `chmod +x deploy/*.sh` if you prefer calling them directly.
- `/etc/econdelta.env` is owned `root:adnan` mode `0640`. The service user reads it at runtime.
- Logs and data directories are preserved across uninstall runs. To fully reset, remove them manually.
