# EconDelta

A deterministic Python pipeline that scrapes Bangladesh Bank, DSE, and commodity price data on a schedule, writes versioned JSON snapshots, and exposes a canonical `data/latest.json` consumed by The Brief agent.

## Architecture

```
                   systemd timers (VPS)
                         |
          +--------------+--------------+
          |              |              |
   scrapers/          scrapers/      scrapers/
   bb_forex.py      dse_market.py  commodity_prices.py
          |              |              |
          +--------------+--------------+
                         |
                  aggregate_latest.py
                         |
                  data/latest.json  <-- The Brief agent reads this
                         |
                  utils/notifier.py  --> Discord #econdelta-alerts
```

Anomaly checks (`utils/anomaly.py`) gate every write: if a scraped value deviates beyond the configured threshold, the write is skipped and an alert fires.

## Install (laptop)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

## Run a scraper manually

```bash
python -m scrapers.bb_forex
```

## Run the aggregator

```bash
python -m aggregate_latest
```

## Run tests

```bash
pytest
# With coverage:
pytest --cov=utils --cov-report=term-missing
```

## VPS deployment

Systemd unit files and `deploy/install.sh` are written in Phase 6. The installer sets `MemoryMax=500M` and `CPUQuota=50%` on each timer unit.

## Environment variables

Copy `.env.example` to `.env` and fill in values. The pipeline reads env vars at import time via `os.environ.get`.

## Trading-day calendar

`config/holidays_2026.json` lists Bangladesh public holidays and the note about moon-sighted religious holidays. Update annually:

1. Verify dates against the official DSE trading calendar published each year.
2. Add/remove entries under the `"holidays"` key.
3. Bump `"last_reviewed"` to today.

The `utils/calendar.py` module reads this file via `load_holidays()`.

## Status values in `latest.json`

| Value     | Meaning                                              |
|-----------|------------------------------------------------------|
| `ok`      | Scraped successfully within expected freshness window |
| `stale`   | Last successful scrape is older than freshness window |
| `failed`  | Most recent scrape attempt raised an exception        |
| `missing` | No snapshot exists yet (first run or data wiped)      |
