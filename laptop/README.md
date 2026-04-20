# Laptop-side scrapers (Option A: hybrid deploy)

## Why this exists

Helsinki/Hetzner VPS IPs are blocked by both Bangladesh Bank (Radware bot challenge)
and DSE (TCP-level block). Scrapers for those sources must run from a Bangladesh
residential IP — i.e. Adnan's laptop.

Commodity (yfinance) and the aggregator stay on VPS.

## Flow

```
Laptop (BDT local time)          VPS (UTC)
-----------------------          --------
06:05 bb_forex runs              00:08 commodity_prices runs
  → rsync to VPS                 00:20 aggregate_latest runs (morning)
  → trigger VPS aggregator       → data/latest.json refreshed
                                 10:35 aggregate_latest runs (afternoon)
16:30 dse_market runs
  → rsync to VPS
  → trigger VPS aggregator
```

## Files

| File | Purpose |
|---|---|
| `run-and-sync.sh` | Wrapper: activate venv, run scraper, rsync to VPS, trigger remote aggregator |
| `com.clauding-lab.econdelta.bb-forex.plist` | launchd plist — bb_forex at 06:05 BDT daily |
| `com.clauding-lab.econdelta.dse-market.plist` | launchd plist — dse_market at 16:30 BDT daily |

Secrets (Discord webhook, VPS host, repo path) live in `~/.econdelta.env` (chmod 600, NOT in git).

## Install

```bash
# Copy plists to LaunchAgents
cp laptop/com.clauding-lab.econdelta.*.plist ~/Library/LaunchAgents/

# Load (boots immediately into launchd scheduler)
launchctl load ~/Library/LaunchAgents/com.clauding-lab.econdelta.bb-forex.plist
launchctl load ~/Library/LaunchAgents/com.clauding-lab.econdelta.dse-market.plist

# Verify loaded
launchctl list | grep econdelta
```

## Manual trigger (for testing)

```bash
# Run now (doesn't wait for scheduled time)
launchctl start com.clauding-lab.econdelta.bb-forex

# Or run the wrapper directly
bash laptop/run-and-sync.sh bb_forex
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.clauding-lab.econdelta.bb-forex.plist
launchctl unload ~/Library/LaunchAgents/com.clauding-lab.econdelta.dse-market.plist
rm ~/Library/LaunchAgents/com.clauding-lab.econdelta.*.plist
```

## Caveats

- **Laptop must be awake and connected at scheduled time.** If the laptop is asleep at 06:05 BDT, launchd queues the job for when it wakes. That may delay the morning snapshot by minutes to hours. The aggregator on VPS uses the staleness logic (`age_hours > 24`) to flag data freshness.
- **No DST in Bangladesh** — UTC+6 year-round, so scheduled local hours stay correct.
- **Network required** — laptop offline = scraper runs but rsync fails; wrapper logs warning.
- **No retry logic yet** — a failed scrape is not auto-retried. Future improvement: wrapper detects exit 1 and re-enqueues via `launchctl submit`.

## Logs

- `logs/launchd-bb-forex.log` — combined stdout/stderr from the wrapper (scrape + rsync + aggregator trigger output)
- `logs/launchd-bb-forex.stdout` / `.stderr` — launchd's direct capture (usually empty; wrapper redirects)

Tail during a manual test:

```bash
tail -f logs/launchd-bb-forex.log
```
