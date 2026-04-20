# EconDelta Integration Contract

> **Audience:** agents that consume EconDelta data (primarily **The Brief**, future **YieldScope**, any downstream dashboard).
>
> **Stability:** schema v1.0. Breaking changes will bump `schema_version` and be announced via a git tag + Discord notice.

---

## The one file you read

```
/home/adnan/econdelta/data/latest.json
```

On the VPS (Hetzner, `135.181.43.68`). This is the **canonical, most-recent-possible** snapshot of every metric EconDelta tracks.

The file is **atomically written** (`.tmp` + `os.replace`) by `aggregate_latest.py`. A reader will never see a partial file. Concurrent reads are safe.

Refresh cadence:
- Full morning refresh by **00:25 UTC** (06:25 BDT) — commodity_prices scrape + bb_forex rsync-from-laptop + aggregate all complete
- Full afternoon refresh by **10:40 UTC** (16:40 BDT) — dse_market rsync-from-laptop + aggregate
- Plus any laptop-triggered refresh after a manual scrape

---

## Shape: `LatestBundle`

Source of truth: `utils/schema.py` → `LatestBundle` (Pydantic v2).

```json
{
  "schema_version": "1.0",
  "updated_at": "2026-04-20T17:21:34.065677Z",
  "sources_status": {
    "bb_forex":         { "status": "ok", "last_success": "2026-04-20T17:21:24Z", "age_hours": 0.0, "url": "https://www.bb.org.bd/en/index.php/econdata/exchangerate", "error": null },
    "dse_market":       { "status": "ok", "last_success": "2026-04-20T15:54:56Z", "age_hours": 1.44, "url": "https://www.dse.com.bd/market-statistics.php", "error": null },
    "commodity_prices": { "status": "ok", "last_success": "2026-04-20T17:07:03Z", "age_hours": 0.24, "url": null, "error": null }
  },
  "data": { ... flat metric dict; see below ... }
}
```

### `schema_version`
String. Currently `"1.0"`. Breaking changes bump the major. Readers **should** assert the version they expect and fail loudly otherwise — better than silently misreading.

### `updated_at`
ISO-8601 UTC datetime. Always timezone-aware (`Z` suffix or `+00:00`). This is when the aggregator last ran, **not** when any individual scraper ran — for scraper freshness, read `sources_status.<source>.last_success`.

### `sources_status`
One entry per source. Always these three keys (even if a source is missing): `bb_forex`, `dse_market`, `commodity_prices`.

Each entry is a `SourceStatus`:

| Field | Type | Meaning |
|---|---|---|
| `status` | `"ok" \| "stale" \| "failed" \| "missing"` | See status values below |
| `last_success` | ISO-8601 UTC `\| null` | When the snapshot was successfully written. `null` only for `missing`. |
| `age_hours` | float `\| null` | Hours between `last_success` and aggregator's `updated_at`. `null` for `missing`. Rounded to 2 decimals. |
| `url` | string `\| null` | Source URL, for traceability. `null` for commodity_prices (provider API, no URL). |
| `error` | string `\| null` | Human-readable error for `failed`/`missing`. `null` when healthy. |

### `data`
Flat dict of all available metrics. **Presence depends on source health** — a missing or failed source means its fields are absent. Always check `sources_status[source].status == "ok"` before trusting fields derived from that source.

---

## `status` values — what each means

| Value | When | What a consumer should do |
|---|---|---|
| `ok` | Snapshot exists, `age_hours <= 24` | Trust the data. |
| `stale` | Snapshot exists, `age_hours > 24` | Render the value but flag visually (e.g. 🕐 icon, "data from N hours ago"). **Don't** compute deltas from a stale value without noting it. |
| `failed` | Reserved for future use (explicit scraper-written failure sentinel) | Treat like `missing`. Inspect `error`. |
| `missing` | No snapshot file found for this source at all | Omit the source from output, or display "data unavailable" explicitly. Never hallucinate a value. |

### The staleness rule, concretely

`STALE_THRESHOLD_HOURS = 24.0` (constant in `aggregate_latest.py`). A source that was last successful 23 hours ago is `ok`; 25 hours ago is `stale`.

Weekend/holiday caveats:
- **DSE** doesn't trade Fri/Sat, so on Fri/Sat/Sun morning, DSE data is by design >24h old and will be `stale`. This is expected; consumers should not treat DSE-stale as an error on non-trading days. Use `data.trading_day` to disambiguate.
- **BB forex** updates daily including Fri/Sat (bank rates still post), so `stale` there does mean something broke.
- **Commodity** pulls global markets — no BD holiday exemption.

---

## `data` fields reference (v1.0)

All fields are optional at the JSON level — a reader **must** handle `KeyError` / missing keys. The best pattern:

```python
latest = json.load(open("/home/adnan/econdelta/data/latest.json"))
forex_ok = latest["sources_status"]["bb_forex"]["status"] == "ok"
usd_bdt = latest["data"].get("usd_bdt_mid") if forex_ok else None
```

### From `bb_forex` (present when `sources_status.bb_forex.status == "ok"`)

| Key | Type | Unit | Notes |
|---|---|---|---|
| `usd_bdt_mid` | float | BDT per 1 USD | Maps to BB's "WAR" (Weighted Average Rate) |
| `usd_bdt_buy` | float | BDT per 1 USD | BB "bid rate" |
| `usd_bdt_sell` | float | BDT per 1 USD | BB "ask rate" |
| `eur_bdt` | float | BDT per 1 EUR | Mid of BB bid/ask |
| `gbp_bdt` | float | BDT per 1 GBP | Mid of BB bid/ask |
| `gross_reserves_usd_bn` | float | USD billion | Converted from BB's published USD millions |
| `import_cover_months` | `float \| null` | months | **Always `null` in v1** — BB intreserve page doesn't publish this. Schema reserves the field. |
| `reserves_date` | ISO date string | YYYY-MM-DD | First of the month that reserves figure represents. Reserves publish **monthly**, not daily — don't expect this to change every day. |

### From `dse_market` (present when `sources_status.dse_market.status == "ok"`)

| Key | Type | Unit | Notes |
|---|---|---|---|
| `trading_day` | bool | — | `false` on Fri/Sat/BD public holidays; when `false`, the index/market fields below will be **absent**. Always check this first. |
| `dsex` | float | index | Dhaka Stock Exchange broad index |
| `dsex_change` | float | points | Absolute change vs previous trading day |
| `dsex_change_pct` | float | percent | Same change as a percent (e.g. `-0.287` = -0.287%, NOT -28.7%) |
| `ds30` | `float \| null` | index | DSE 30 blue-chip index |
| `dses` | `float \| null` | index | DSE Shariah index |
| `turnover_crore` | float | BDT crore (10M) | Daily trading turnover. Converted from DSE's published Taka. |
| `total_trades` | int | count | |
| `advancing` | int | count of issues | |
| `declining` | int | count of issues | |
| `unchanged` | int | count of issues | |

### From `commodity_prices` (present when `sources_status.commodity_prices.status == "ok"`)

| Key | Type | Unit | Notes |
|---|---|---|---|
| `brent_crude_usd_barrel` | float | USD / barrel | From `yfinance` ticker `BZ=F` |
| `wti_crude_usd_barrel` | float | USD / barrel | From `CL=F` |
| `gold_usd_oz` | float | USD / troy ounce | From `GC=F` |
| `commodity_change_pct` | `dict[str, float]` | percent as fraction | Keys: `brent_crude`, `wti_crude`, `gold`. Values like `0.0377` = +3.77%. Only includes commodities with a prior close to compare against. |

**Palm oil (`FCPO.KL`) is deferred in v1** — Yahoo Finance returns 404 for the symbol; scraper handles the partial-fetch gracefully. When a replacement ticker lands, field name will be `palm_oil_myr_ton` and it will be added without a schema bump.

---

## Consumer patterns

### 1. Safe read with staleness check

```python
import json
from datetime import datetime, timezone
from pathlib import Path

LATEST = Path("/home/adnan/econdelta/data/latest.json")

def read_econdelta() -> dict:
    """Return parsed latest.json. Caller checks sources_status."""
    with LATEST.open() as f:
        bundle = json.load(f)
    assert bundle["schema_version"] == "1.0", f"schema version mismatch: {bundle['schema_version']}"
    return bundle

def is_source_fresh(bundle: dict, source: str) -> bool:
    s = bundle["sources_status"].get(source, {})
    return s.get("status") == "ok"

def staleness_label(bundle: dict, source: str) -> str:
    """Human-readable staleness tag for display."""
    s = bundle["sources_status"].get(source, {})
    if s.get("status") == "ok":
        return ""
    if s.get("status") == "missing":
        return "(data unavailable)"
    age = s.get("age_hours") or 0
    return f"(from {age:.0f}h ago)"
```

### 2. Building The Brief's KEY METRICS SNAPSHOT table

```python
def metrics_row(bundle: dict, label: str, key: str, prev_key: str | None = None, fmt: str = "{:.2f}") -> str:
    data = bundle["data"]
    cur = data.get(key)
    if cur is None:
        return f"| {label} | — | — | — |"
    prev = data.get(prev_key) if prev_key else None
    delta = "" if prev is None else f"{((cur - prev) / prev * 100):+.2f}%"
    return f"| {label} | {fmt.format(cur)} | {fmt.format(prev) if prev else '—'} | {delta} |"
```

(The Brief would maintain its own previous-day snapshot for diffing; EconDelta does not currently retain >1 day of `latest.json` history.)

### 3. Guarding against non-trading days

```python
data = bundle["data"]
if bundle["sources_status"]["dse_market"]["status"] == "ok":
    if data.get("trading_day") is False:
        # DSE has no data today — don't render the DSE section or render "markets closed"
        ...
    else:
        dsex = data["dsex"]
        ...
```

---

## Error handling contract

### What EconDelta guarantees

1. `latest.json` is either fully valid Pydantic-round-tripped JSON matching `LatestBundle`, or **the write is aborted and the previous file is left untouched**. You will never see a half-written file.
2. `schema_version` is always present.
3. `sources_status` always has exactly the three keys: `bb_forex`, `dse_market`, `commodity_prices`.
4. Missing/failed sources surface via `sources_status[*].status`, not by omitting the source.
5. On any aggregator validation failure, Discord alerts fire and the old `latest.json` is retained.

### What EconDelta does **not** guarantee

1. That any specific `data.*` field exists. Always use `.get()` or try/except. The schema-validated `LatestBundle.data` is typed as `dict[str, Any]` by design — it's a flat pass-through.
2. That all sources are fresh at the same moment. Commodity pulls at 00:08 UTC; DSE can be 12+ hours older when morning brief runs.
3. That historical data is retained — `data/latest.json` only ever reflects the most recent aggregate. Per-scraper history is in `data/{bb_forex,dse_market,commodity_prices}/YYYY-MM-DD.json`, but those paths are **not** a stable consumer contract.

### If EconDelta is unhealthy

- `latest.json` missing entirely → EconDelta never ran. Consumer should halt with a clear error ("EconDelta pipeline offline") rather than fabricate data.
- All three sources `missing`/`failed` → infrastructure failure. Same handling.
- `schema_version` mismatch → EconDelta upgraded; consumer needs to update its reader.

Discord `#econdelta-alerts` is the health channel — a missing morning alert + a stale `latest.json` both mean something's wrong with the VPS or the laptop launchd.

---

## Operational context for consumers

### Where EconDelta's data actually comes from

(This is **not** a stable part of the contract, just orientation for debugging.)

- **bb_forex** — scraped on Adnan's laptop at 06:05 BDT (Playwright + stealth, passes Radware WAF). Rsync'd to VPS. Not runnable from Hetzner directly.
- **dse_market** — scraped on laptop at 16:30 BDT (requests+BS4, DSE is static). Rsync'd to VPS. Also not runnable from Hetzner (TCP-level ban).
- **commodity_prices** — scraped directly on VPS at 00:08 UTC via yfinance.
- **aggregator** — runs on VPS at 00:20 UTC + 10:35 UTC + triggered after each laptop rsync.

### Known limitations (v1)

- Reserves update monthly, not daily. `reserves_date` tells you which month.
- Palm oil not tracked (ticker dead).
- Import cover months not published by BB, always `null`.
- Weekend freshness: DSE is structurally stale Fri-Sun mornings (by design). BB forex should stay fresh daily.
- No T-bill/bond yields, no auction data (deferred from full PRD; coming in v2).
- No BB call money / repo rates (deferred).

### Schema evolution policy

- **v1.x** (minor bump): additive fields only. Existing keys, types, and semantics are immutable.
- **v2.0** (major bump): anything goes. Announced via git tag + Discord.

Readers should assert `bundle["schema_version"].startswith("1.")` for forward-compat with v1.x additions.

---

## Quick reference card

```
Path:       /home/adnan/econdelta/data/latest.json
Schema:     v1.0
Refresh:    00:25 UTC (morning), 10:40 UTC (afternoon) + event-driven
Stale at:   age_hours > 24
Health:     Discord #econdelta-alerts
Code:       github.com/clauding-lab/econdelta (private)
Source:     utils/schema.py → LatestBundle, SourceStatus
```
