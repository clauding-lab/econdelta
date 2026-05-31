[![EconDelta — Bangladesh's autonomous macro data pipeline](https://raw.githubusercontent.com/clauding-lab/econdelta/badges/hero.svg)](https://econdelta.clauding-lab.com)

# EconDelta

> **Bangladesh's macroeconomy, captured autonomously — every day.**
> A self-running pipeline that scrapes, parses, reconciles and archives
> Bangladesh's economic indicators into one queryable repository.

[![data points](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fclauding-lab%2Fecondelta%2Fbadges%2Fdatapoints.json)](https://econdelta.clauding-lab.com)
[![history](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fclauding-lab%2Fecondelta%2Fbadges%2Fbacklog.json)](https://econdelta.clauding-lab.com)
[![indicators](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fclauding-lab%2Fecondelta%2Fbadges%2Findicators.json)](docs/indicator-catalog.md)
[![data updated](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fclauding-lab%2Fecondelta%2Fbadges%2Fupdated.json)](https://econdelta.clauding-lab.com)
[![deploy](https://github.com/clauding-lab/econdelta/actions/workflows/pwa-deploy.yml/badge.svg)](https://github.com/clauding-lab/econdelta/actions/workflows/pwa-deploy.yml)

🌐 **Live dashboard → [econdelta.clauding-lab.com](https://econdelta.clauding-lab.com)**

EconDelta is the **data layer** for Bangladesh economic indicators.
It scrapes ~70 series from Bangladesh Bank, BBS, NBR, DSE, DAM, and
news sources on a daily schedule; parses them; and writes both a
canonical `data/latest.json` snapshot and a row-per-indicator-per-day
into Supabase `metric_history` for queryable warm history. A deep
`metric_history_monthly` archive carries the same indicators back to
**January 2012** — roughly 14 years of backlog (10,000+ data points
and counting across both tables).

**If you're a downstream app** (the brief, Mission Control, Notifyr,
something new) and you want to *read* this data, you do **not** need
to depend on EconDelta's Python code. The contract you depend on is:

1. The Supabase `metric_history` table (schema in
   [`db/schema.sql`](db/schema.sql))
2. The indicator catalog (browseable at
   [`docs/indicator-catalog.md`](docs/indicator-catalog.md))
3. The consumer guide ([`docs/data-contract.md`](docs/data-contract.md))

Read those three; ignore the rest of this repo. EconDelta's internal
scrapers, parsers, anomaly gates, and aggregator orchestration are
implementation details that may evolve — the table schema and
indicator IDs are the stable interface.

## Architecture

```
                  systemd timers @ ExonVPS (BDIX, Dhaka)
                       │   05:00–06:10 BDT daily
              ┌────────┼────────┬─────────┬──────────┐
              ↓        ↓        ↓         ↓          ↓
        scrapers/ scrapers/ scrapers/  parsers/   parsers/
        bb_forex  dse_mkt   commodity  hybrid     dam_ticker
              │        │        │         │          │
              └────────┴────────┴─────────┴──────────┘
                                │
                       aggregate_latest.py
                                │
              ┌─────────────────┼─────────────────┐
              ↓                 ↓                 ↓
       data/latest.json  data/archive/    Supabase metric_history
       (today's snap)    <date>.json      (warm queryable history)
                         (cold backup)             ↑
                                                   │ read-only
                                                   │
                                              The Brief / future apps
```

Anomaly checks (`utils/anomaly.py`) gate every write: if a scraped value
deviates beyond the configured threshold, the write is skipped and an
alert fires (`utils/notifier.py` → Discord `#econdelta-alerts`).

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
