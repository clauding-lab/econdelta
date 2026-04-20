# anatomy.md

> Auto-maintained by OpenWolf. Last scanned: 2026-04-20T15:44:12.855Z
> Files: 25 tracked | Anatomy hits: 0 | Misses: 0

## ./

- `.gitignore` — Git ignore rules (~80 tok)
- `CLAUDE.md` — OpenWolf (~57 tok)
- `pyproject.toml` — Python project configuration (~197 tok)
- `README.md` — Project documentation (~631 tok)
- `requirements-dev.txt` (~10 tok)
- `requirements.txt` — Python dependencies (~45 tok)

## .claude/

- `settings.json` (~441 tok)

## .claude/rules/

- `openwolf.md` (~313 tok)

## config/

- `holidays_2026.json` (~204 tok)
- `sources.json` — Declares on (~1120 tok)
- `thresholds.json` (~161 tok)

## data/

- `.gitkeep` (~0 tok)

## docs/

- `DECISION-LOG.md` — EconDelta — Decision Log (~1389 tok)

## logs/

- `.gitkeep` (~0 tok)

## scrapers/

- `__init__.py` (~0 tok)
- `bb_forex.py` — Playwright-driven BB forex rates + reserves scraper; anomaly-gated; atomic JSON write; exit codes 0/1/2. (~320 tok)
- `commodity_prices.py` — yfinance commodity scraper with anomaly gating and schema validation. Fetches Brent/WTI/Gold/Palm Oil, writes versioned JSON snapshots. (~270 tok)
- `dse_market.py` — DSE daily market scraper (requests+BS4): homepage for DSEX/DS30/DSES, market-statistics.php for breadth/turnover. Anomaly-gated, trading-day-gated, atomic write to data/dse_market/. Exit codes 0/1/2. (~250 tok)

## tests/

- `__init__.py` (~0 tok)
- `conftest.py` — Shared pytest fixtures. (~168 tok)
- `test_bb_forex.py` — 25 unit tests for scrapers/bb_forex.py covering parse, reserves mn->bn conversion, atomic write, anomaly gating, main() exit codes 0/1/2. (~450 tok)
- `test_commodity_prices.py` — 7 unit tests for scrapers/commodity_prices.py covering fast_info path, history fallback, FetchError, main() with all/partial/zero fetches, and anomaly exit-2. (~350 tok)
- `test_dse_market.py` — 15 unit tests for scrapers/dse_market.py covering index parsing, turnover conversion, breadth fields, ParseError raises, non-trading-day skip, FetchError exit-1, DSEX anomaly exit-2. (~400 tok)
- `test_aggregator.py` — 17 unit+integration tests for aggregate_latest.py: find_latest_snapshot, compute_status, flatten_data, write_latest atomicity, main() end-to-end, stale-warning, exit-1 on validation failure. (~500 tok)

## tests/fixtures/

- `.gitkeep` (~0 tok)
- `bb_exchange_rates.html` — Live BB exchange rates page HTML snapshot (captured 2026-04-20). (~15k tok)
- `bb_forex_reserves.html` — Live BB intreserve page HTML snapshot (captured 2026-04-20). (~14k tok)
- `dse_homepage.html` — Live DSE homepage HTML snapshot (captured 2026-04-20). (~90k tok)
- `dse_market_statistics.html` — Live DSE market-statistics.php HTML snapshot (captured 2026-04-20). (~2k tok)

## ./

- `aggregate_latest.py` — Phase 5 aggregator: reads latest snapshot from each scraper subdir, flattens to LatestBundle, atomic writes data/latest.json; entry point `python -m aggregate_latest`. (~310 tok)

## data/

- `latest.json` — Canonical bundle consumed by The Brief: schema_version, updated_at, sources_status (ok/stale/missing + age_hours), flat data dict (forex, DSE, commodities). (~200 tok)

## data/bb_forex/

- `2026-04-20.json` — First live snapshot: USD/BDT=122.7, EUR/BDT=144.34, GBP/BDT=165.85, gross_reserves=34.1166bn. (~120 tok)

## utils/

- `__init__.py` (~0 tok)
- `anomaly.py` — Per-metric threshold checks — prevent bad data from corrupting latest.json. (~646 tok)
- `calendar.py` — Bangladesh trading-day calendar. (~709 tok)
- `http_client.py` — Shared HTTP session with retries, timeout, and User-Agent. (~1154 tok)
- `notifier.py` — Discord webhook alerts with rate-limit and dry-run support. (~950 tok)
- `parser.py` — HTML parsing helpers for BB/DSE tables. (~1126 tok)
- `schema.py` — Pydantic models for scraper snapshots and latest.json bundle. (~960 tok)

## deploy/

- `econdelta-forex.service` — systemd oneshot for bb_forex scraper; MemoryMax=500M, CPUQuota=50%, ProtectSystem=strict, TimeoutStartSec=300. (~220 tok)
- `econdelta-dse.service` — systemd oneshot for dse_market; TimeoutStartSec=60. (~220 tok)
- `econdelta-commodity.service` — systemd oneshot for commodity_prices; TimeoutStartSec=120. (~220 tok)
- `econdelta-aggregate.service` — systemd oneshot for aggregate_latest; MemoryMax=250M, TimeoutStartSec=30. (~220 tok)
- `econdelta-forex.timer` — OnCalendar 00:05 UTC daily. (~60 tok)
- `econdelta-commodity.timer` — OnCalendar 00:08 UTC daily. (~60 tok)
- `econdelta-dse.timer` — OnCalendar 10:30 UTC daily (scraper handles non-trading-day skip). (~60 tok)
- `econdelta-aggregate.timer` — OnCalendar 00:20 + 10:35 UTC (two runs). (~70 tok)
- `install.sh` — Idempotent sudo installer: copies units to /etc/systemd/system, enables timers, creates /etc/econdelta.env stub. (~400 tok)
- `uninstall.sh` — Disables timers, removes units, preserves env+logs+data. (~150 tok)
- `logrotate.conf` — Daily rotation, keep 30 days, copytruncate for log files. (~50 tok)
- `README.md` — Operational runbook: first-time install, verify, manual run, update, rollback, schedule table. (~400 tok)
