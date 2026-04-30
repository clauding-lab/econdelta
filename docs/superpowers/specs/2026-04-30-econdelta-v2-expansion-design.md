# EconDelta v2 — Bangladesh Indicators Expansion

**Status:** Draft (design)
**Date:** 2026-04-30
**Author:** Adnan + Claude Code (brainstorming session)
**Implements:** ~45 Bangladesh economic indicators sourced from `config/sources-v2.json`
**Supersedes (partial):** the 9-value MVP shape (`bb_forex` + `dse_market` + `commodity_prices`)

---

## 1. Purpose

EconDelta today exposes 9 numeric values to The Brief. The HoSME-derived backlog in `config/sources-v2.json` enumerates ~45 Bangladesh economic indicators across 9 domains (forex, money market, monetary aggregates, inflation, government finance, external sector, commodities, equities, macro). This spec defines the architecture, schema, scheduling, and one-time first-extraction sequence to ship that expansion in a single coordinated change.

The expansion ships in one tranche (Option A from brainstorm) — not phased — because the user prefers to validate the full pipeline end-to-end up front rather than iterate domain by domain.

## 2. Goals & non-goals

**Goals:**
1. Extract every URL-fetchable indicator in `sources-v2.json` (~40 of 45).
2. Reuse the existing per-indicator JSON snapshot shape (`data/<indicator>/<date>.json`) and aggregator pattern.
3. Decouple fetch (network-fragile, IP-restricted) from parse (deterministic, replayable from cache).
4. Use a hybrid parser: deterministic-first, with Sonnet 4.6 (via Claude Max headless `claude -p`) as fallback and as a sanity-check on every successful deterministic parse.
5. Reorganize `latest.json` into 9 domain groups while preserving the legacy MVP keys for The Brief read-path compatibility.
6. Run a one-time first-extraction today on ExonVPS to validate the full pipeline.

**Non-goals:**
- Migrating The Brief itself to ExonVPS (separate, deferred).
- Replacing news-sourced indicators (5 of 45) with automated news/LLM extraction. v3 falls back to their `alternate` PDFs.
- Reworking anomaly thresholds globally — they move from `config/thresholds.json` into per-indicator entries in `sources-v3.json`, but the values are inherited from MVP defaults at first.
- Adding new indicators beyond the HoSME list.

## 3. Architecture

Three-stage pipeline:

```
┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│  Stage 1: FETCH      │  │  Stage 2: PARSE      │  │  Stage 3: EMIT       │
│  ───────────────────  │  │  ───────────────────  │  │  ───────────────────  │
│  • download PDFs      │→ │  • deterministic try  │→ │  • per-indicator JSON │
│    to local cache     │  │    (pdfplumber)       │  │    snapshot           │
│  • scrape HTML pages  │  │  • LLM fallback       │  │  • anomaly check      │
│    via playwright     │  │    (claude -p Sonnet) │  │  • aggregator picks   │
│  • cache by hash      │  │  • LLM sanity-check   │  │    them up into       │
│    + retain history   │  │    on every parse     │  │    latest.json        │
└──────────────────────┘  └──────────────────────┘  └──────────────────────┘
       │                          │                          │
       ▼                          ▼                          ▼
 data/_pdfs/                 data/<indicator>/           data/latest.json
 <source>/<date>/*.pdf       <date>.json                 (extended schema)
```

**Stage 1 (Fetch)** is the network-fragile, IP-restricted, deterministic-IO step. It downloads or scrapes everything `sources-v3.json` points at, caches the raw artifacts in `data/_pdfs/<source>/<YYYY-MM>/` and `data/_html/<source>/<date>.html`, and exits. Output: raw PDFs + raw HTML on disk.

**Stage 2 (Parse)** is offline. Reads from the artifact caches, runs deterministic parsers (pdfplumber for PDF tables, BeautifulSoup for HTML); when the parser fails or its result fails sanity bounds, falls back to `claude -p` with Sonnet 4.6 reading the same artifact. Every successful deterministic parse also gets a Sonnet sanity-check before being trusted.

**Stage 3 (Emit)** writes per-indicator snapshots and re-aggregates `latest.json`. Unchanged in shape from the existing scrapers, but now driven by the registry rather than three hardcoded sources.

**Why split fetch and parse:** if a PDF download fails (network), Stage 2 can re-try parsing yesterday's cached PDF. If a parse breaks (BB layout change), Stage 1 isn't re-run pointlessly. If the LLM call fails (Claude Max quota), the deterministic value still lands.

## 4. Source registry — `config/sources-v3.json`

`sources-v2.json` today is documentation. v3 becomes **executable configuration** that drives the pipeline.

Per-indicator entry shape:

```json
{
  "id": "policy_rate",
  "name": "Policy Rate",
  "domain": "money_market",
  "cadence": "daily",
  "schedule": "00:10 UTC",
  "fetch": {
    "type": "html",
    "url": "https://www.bb.org.bd/en/",
    "selector_hint": "footer ticker — last value of the page"
  },
  "parse": {
    "deterministic": "html_footer_ticker",
    "llm_prompt": "policy_rate.txt",
    "value_type": "percent",
    "valid_range": [0.5, 25.0]
  },
  "anomaly_threshold": 1.0,
  "alternate": { "type": "pdf", "url": "...", "task": "..." },
  "fallback": null
}
```

**New fields versus v2:**

| Field | Why |
|---|---|
| `domain` | Drives which subsection of `latest.json` the indicator lands in |
| `cadence` + `schedule` | Tells the timer system whether to run daily/weekly/monthly |
| `fetch.type` | One of `html`, `pdf` — selects the Stage 1 fetcher |
| `fetch.discover` | For PDFs, says "find the latest PDF on this index page" — generic discovery helper |
| `parse.deterministic` | Name of a registered parser strategy — small set of reusable patterns, not 40 unique parsers |
| `parse.llm_prompt` | Prompt template filename used as fallback + sanity-check |
| `parse.value_type` + `valid_range` | Hard validation gate — catches "got 0" or "got 9999999" failures regardless of source |
| `anomaly_threshold` | Day-on-day or month-on-month % change ceiling (replaces global `thresholds.json`) |

**Domain groupings (drives `latest.json` shape):**

- `forex_and_reserves` — USD/BDT, EUR/BDT, GBP/BDT, INR/BDT, FX reserves gross/BPM6, FX buy/sale
- `money_market` — policy rate, SLF, SDF, call money, repo, GSEC auction/maturity, T-bill/bond outstanding + rates
- `monetary_aggregates` — broad money, reserve money, currency outside bank, deposits, CRR, money multiplier, excess liquidity, NSC, private sector credit
- `inflation` — point-to-point, general, food, non-food
- `government_finance` — tax revenue, non-tax revenue, total revenue, tax-GDP ratio, rev-GDP ratio, budget OpEx/ADP-Ex, deficit financing components
- `external_sector` — exports (monthly + FY + categorywise), imports + LC opening/settlement, remittance (monthly + FY + by country), BOP summary
- `commodities` — Brent, WTI, gold (no schema change)
- `equities` — DSEX, DS30, breadth (no schema change)
- `macro` — GDP

**Migration v2 → v3:** programmatic. A small one-shot script reads `sources-v2.json` and emits `sources-v3.json` with reasonable defaults, then hand-tune (set `domain`, `value_type`, `valid_range`, `anomaly_threshold`). Existing `sources.json` (the live MVP) gets folded in as the seed entries.

## 5. Stage 1 — Fetch infrastructure

```
econdelta/
  fetchers/
    __init__.py
    base.py              # FetchResult dataclass, common errors
    html_fetcher.py      # playwright-stealth wrapped
    pdf_fetcher.py       # downloads + caches + dedupes by content hash
    pdf_discovery.py     # "find latest PDF link on this index page"
  data/
    _pdfs/
      bb_mei/                    # Major Economic Indicators (publictn/3/11)
        2026-04/
          mei-april-2026.pdf
          .meta.json              # source_url, fetch_ts, sha256, page_count
      bb_monthly_econ/            # Monthly Economic Indicators (publictn/5/27)
      bb_govt_sec/                # Govt Securities Annex (publictn/3/58)
      bbs_inflation/
      mof_budget/
    _html/
      bb_treasury/
        2026-04-30.html           # raw snapshot for replay
```

**`pdf_fetcher.py` responsibilities:**
1. Take a `fetch.url` (direct PDF link or index page).
2. If `fetch.discover` is set, call `pdf_discovery.py` to find the latest PDF link on the index page.
3. Download with playwright (same stealth context BB scraping needs).
4. Compute SHA-256 of bytes; if a previous fetch has the same hash, skip writing and emit a "no-op, content unchanged" marker (saves disk + signals downstream that re-parse is unnecessary).
5. Write to `data/_pdfs/<source>/<YYYY-MM>/<filename>.pdf` + a `.meta.json` sidecar with: source URL, fetch timestamp (UTC), sha256, page count, file size.

**`pdf_discovery.py`** — generic helper. BB publication pages are listings ("April 2026 / March 2026 / ..."). Pattern: take the index URL, find anchor tags whose text matches the most recent month, return absolute URL. A single CSS-selector + regex helper covers all 4 BB publication families.

**`html_fetcher.py`** — thin wrapper over the existing `bb_forex.py` playwright code. Returns rendered HTML as a string + writes a snapshot. Stealth context is shared singleton (one Chromium launch per Stage 1 run).

**`base.py`:**

```python
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class FetchResult:
    indicator_id: str
    artifact_path: Path
    artifact_type: Literal["pdf", "html"]
    fetched_at: datetime
    source_url: str
    sha256: str
    cache_hit: bool
```

**Error handling at this stage:**
- Network timeout / 5xx → retry 2× with exponential backoff (30s, 90s), then mark `fetch_failed` and continue.
- Bot-challenge detected (Radware fingerprint in HTML) → log + Discord alert. Should never fire from ExonVPS but defensive.
- PDF discovery returns no link → log + Discord alert. Usually means BB redesigned the index.

**Stage 1 entry point:** `python -m econdelta.fetch_all`. Exit summary: `Fetched: N · Cache hits: M · Failed: K`.

## 6. Stage 2 — Parse infrastructure (hybrid + LLM sanity-check)

```
econdelta/
  parsers/
    __init__.py
    base.py                   # ParseResult, ParseError, validation helpers
    registry.py               # name → parser-callable lookup
    pdf_component.py          # BB-style "Component 12c" cell extraction
    pdf_table_row.py          # "Page 15, first table, last row"
    pdf_table_total.py        # "last total of the page"
    html_footer_ticker.py     # bb.org.bd footer-ticker (USD/BDT, policy rate, SLF, SDF)
    html_table_row.py         # gsom.bb.org.bd MTM-bill / MTM-bond style
    html_call_money.py        # 1D/7D/14D/90D rate table
  claude/
    __init__.py
    max_client.py             # subprocess wrapper for `claude -p` (Sonnet 4.6 default)
    validators.py             # value_type sanity, range checks
    prompts/
      pdf_component.txt
      pdf_table_row.txt
      html_footer_ticker.txt
      sanity_check.txt
```

**Hybrid flow per indicator:**

```
1. Load FetchResult artifact + indicator config
2. Try deterministic parser (registry[parse.deterministic])
   ├─ Success → got value V_det
   │    └─ Validate: value_type ok? in valid_range?
   │         ├─ Yes → call Sonnet sanity-check
   │         │         ├─ Sonnet agrees → emit V_det as authoritative
   │         │         └─ Sonnet disagrees → call Sonnet extract → V_llm
   │         │              ├─ V_llm matches V_det within tolerance¹ → emit V_det (Sonnet's earlier "implausible" was noise)
   │         │              └─ V_llm differs → flag "needs_review", emit BOTH, Discord alert
   │         └─ No → fall through to LLM extract
   └─ Failure (exception) → fall through to LLM extract
3. LLM extract path: call Sonnet with artifact + llm_prompt → V_llm
   ├─ Validates ok → emit V_llm with provenance="llm_extracted"
   └─ Validates fail → emit "extract_failed", Discord alert
```

¹ "Within tolerance" is per-indicator: floats within 0.5% relative diff (or absolute equality if `value_type` is `int`/`enum`). Defined in `parsers/base.py:values_match()`.

**Provenance is recorded** in every snapshot — `_provenance: "deterministic" | "llm_extracted" | "llm_corrected" | "needs_review"`. Single biggest debugging affordance.

**Sonnet sanity-check is cheap** — pass: indicator name, deterministic value, valid range, last 3 known values. Sonnet returns `{"plausible": true|false, "reason": "..."}`. ~200 input tokens, ~50 output.

**Sonnet extract is heavier** — passes the relevant PDF page text (extracted via pdfplumber `page.extract_text()`) plus the human-readable instruction from sources-v3. ~2-4k input tokens, ~100 output.

**Why text not image:** Sonnet 4.6 reads text fine and is faster + cheaper than vision. BB PDFs are text-encoded, not scanned. Vision is reserved for the rare PDF where text extraction fails.

**`max_client.py` adapted from the-brief:**
- Default model: `claude-sonnet-4-6` (the-brief uses opus by default).
- Default `effort: str = "medium"` (not "high").
- Same `--no-session-persistence`, `--tools ""`, `--permission-mode bypassPermissions`.
- OAuth via `~/.claude/.credentials.json` (Claude Max).
- `CLAUDE_BINARY` env var for absolute path on ExonVPS (systemd PATH).
- Wrap with retry-on-quota-exceeded (catches "Claude Max throttled" stderr, retries once after 60s).

**Parser registry:**

```python
# parsers/registry.py
from typing import Protocol
from .base import FetchResult, ParseResult


class Parser(Protocol):
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult: ...


REGISTRY: dict[str, Parser] = {}


def register(name: str):
    def decorator(cls):
        REGISTRY[name] = cls()
        return cls
    return decorator
```

Adding a new parser pattern = one new file + one decorator, not modifying a switch statement. Registry can lazy-load — only parsers referenced by sources-v3 entries get instantiated.

**Error handling:**
- Deterministic parser raises → caught, fall through to LLM (logged but not alerted).
- LLM call fails (subprocess timeout, non-JSON output, Max quota exhausted) → emit `extract_failed`, continue. Discord alert: "5 of 40 indicators failed today."
- Sonnet says "implausible" with no historical comparison → emit `needs_review`, Discord alert with indicator + value.
- Hard parser bug → traceback to log, Discord alert with stack summary.

**Stage 2 entry point:** `python -m econdelta.parse_all`. Exit summary: `Parsed: N (det:X, llm:Y, review:Z, failed:K)`.

## 7. Stage 3 — Schema & aggregator changes

**Per-indicator snapshot shape (3 new fields):**

```json
{
  "indicator_id": "policy_rate",
  "name": "Policy Rate",
  "domain": "money_market",
  "cadence": "daily",
  "scraped_at": "2026-04-30T10:00:42+06:00",
  "source_url": "https://www.bb.org.bd/en/",
  "value": 10.0,
  "value_type": "percent",
  "previous_value": 10.0,
  "change_pct": 0.0,
  "_provenance": "deterministic",
  "_artifact_sha256": "ab12...",
  "_parse_strategy": "html_footer_ticker"
}
```

**`latest.json` extended shape:**

```json
{
  "schema_version": "3.0",
  "generated_at": "2026-04-30T10:35:00+06:00",
  "domains": {
    "forex_and_reserves": { "usd_bdt": { ... }, "eur_bdt": { ... }, ... },
    "money_market": { ... },
    "monetary_aggregates": { ... },
    "inflation": { ... },
    "government_finance": { ... },
    "external_sector": { ... },
    "commodities": { "brent_crude": {...}, "wti_crude": {...}, "gold": {...} },
    "equities": { "dsex": {...}, "ds30": {...}, "breadth": {...} },
    "macro": { "gdp_quarterly": {...} }
  },
  "freshness": {
    "indicators_total": 45,
    "indicators_fresh": 41,
    "indicators_stale": 3,
    "indicators_failed": 1,
    "by_cadence": {
      "daily":   { "fresh": 9,  "expected": 9 },
      "weekly":  { "fresh": 2,  "expected": 2 },
      "monthly": { "fresh": 28, "expected": 30, "stale_ids": ["bop_summary","categorywise_export"] },
      "quarterly": { "fresh": 1, "expected": 3 },
      "fy":      { "fresh": 1,  "expected": 6 }
    }
  },
  "alerts": [
    { "indicator_id": "wti_crude", "type": "anomaly", "severity": "warn", "value": 107.93, "previous": 99.45, "change_pct": 8.53 }
  ]
}
```

**Backward compatibility for The Brief — dual-shape `latest.json`:**

The Brief currently reads keys like `latest.json["bb_forex"]["usd_bdt_buy"]`. v3 reshape would break that. v3 aggregator emits **both** shapes — `domains.*` for new consumers AND legacy `bb_forex` / `dse_market` / `commodity_prices` keys for The Brief. The Brief continues working unchanged.

A follow-up cleanup PR (after The Brief migration to ExonVPS lands) drops the legacy keys.

**Aggregator changes:**

`aggregate_latest.py` evolves to:
1. Walk every indicator listed in `sources-v3.json` (not 3 hardcoded sources).
2. Find the most recent `data/<indicator>/<date>.json` snapshot per indicator.
3. Apply staleness rules per cadence:

| Cadence | Stale threshold |
|---|---|
| `daily` | > 24h since `scraped_at` |
| `weekly` | > 8 days |
| `monthly` | > 35 days |
| `quarterly` | > 100 days |
| `fy` | > 400 days |

4. Group into 9 domain buckets.
5. Compute `freshness` summary block.
6. Compute `alerts` by replaying anomaly thresholds over the snapshot.
7. Emit dual-shape (legacy + domains) for the compat window.

**Anomaly thresholds reorganized:** flat `config/thresholds.json` is retired; per-indicator `anomaly_threshold` lives in `sources-v3.json`. This is the natural place to tune them after the soak window.

Holiday handling unchanged (`config/holidays_2026.json` still gates DSE).

## 8. Scheduling

Steady-state schedule keeps the existing 4-timer shape with reassigned roles:

| Timer | UTC | BDT | Role |
|---|---|---|---|
| `econdelta-fetch.timer` | 00:00 daily | 06:00 | Stage 1: fetch all due indicators (HTML + PDF discovery + download). Cache-by-hash makes monthly PDFs near-zero cost. |
| `econdelta-parse.timer` | 00:15 daily | 06:15 | Stage 2: parse everything fetched. Hybrid + Sonnet sanity-check. |
| `econdelta-dse.timer` | 10:30 daily | 16:30 | Stage 1+2 for DSE only (afternoon close). |
| `econdelta-aggregate.timer` | 00:30 + 10:35 | 06:30 + 16:35 | Stage 3: re-aggregate `latest.json`. |

**Sonnet calls per day in steady state:** ~12 average (~10 daily indicators sanity-checked + 0.3/day weekly amortized + 1/day monthly amortized). ~360/month. Within Claude Max session quota for one device.

**Cache-hit short-circuit:** if Stage 1 emits `cache_hit=True` for a PDF (sha256 unchanged), Stage 2 copies forward yesterday's snapshot with an updated `scraped_at` and skips re-parse. Monthly indicators effectively cost zero between BB updates.

## 9. First-extraction sequence (one-time validation pass)

Six discrete steps, each with a clear stop point:

**Step 1 — Build `sources-v3.json`** *(local Mac, ~30 min)*
Programmatic v2→v3 conversion, then hand-tune `domain`, `value_type`, `valid_range`, `anomaly_threshold`. Commit + push.

**Step 2 — Build infrastructure** *(local Mac, ~60 min)*
Skeleton (`fetchers/`, `parsers/`, `claude/`), copy `max_client.py` from the-brief and adapt for Sonnet 4.6, write parser registry, write 5-7 parser strategies covering all ~45 indicators. Tests with fixture PDFs + HTML. Commit + push.

**Step 3 — Deploy to ExonVPS** *(ssh, ~10 min)*
`git pull`, install new deps (pdfplumber), confirm `claude` binary on VPS + `~/.claude/.credentials.json`. **Gate: if Sonnet auth on VPS fails, stop here** — likely a Claude Max device-count question.

**Step 4 — Dry-run Stage 1** *(ssh, ~15 min)*
`python -m econdelta.fetch_all --dry-run`. Then real fetch. Verify ~45 artifacts on disk in `data/_pdfs/` + `data/_html/`.

**Step 5 — Dry-run Stage 2** *(ssh, ~30-45 min)*
`python -m econdelta.parse_all --dry-run`. Then real parse. Walk through:
- Per indicator: deterministic ok? Sanity-check agree? `_provenance` distribution.
- Hand-check ~5 random snapshots against the actual source.
- Tighten `valid_range` / `anomaly_threshold` for any `needs_review` flags.

**Step 6 — Aggregate and inspect** *(ssh, ~10 min)*
`python aggregate_latest.py`. Verify all 9 domains populated, freshness numbers add, no regressions in legacy keys, spot-check 5 indicators end-to-end.

**Total wall-clock: ~2.5-3 hours.**

## 10. Failure modes (first-extraction)

| What fails | Action |
|---|---|
| Sonnet auth on ExonVPS (`claude -p` returns "no credentials") | Stop. Set up Claude Max OAuth on ExonVPS — Claude Max device-count question for the user. |
| Some PDFs 403/blocked | If handful, skip + document. If many, suggests a Stage 1 stealth gap. |
| Many `needs_review` flags | Expected. Fix `valid_range` + thresholds in sources-v3 and re-run Stage 2 only (Stage 1 cache held). |
| Aggregator schema breaks The Brief | Roll back: hold v3 `latest.json` behind feature flag, keep emitting v2 shape, fix dual-emit, retry. |

## 11. Risks

- **Claude Max device count.** Adding ExonVPS as a 2nd Max device may exceed plan. **Primary blocker.** Hits in Step 3. Fallback: Mac-side LLM-assist (Shape C) until plan limit confirmed.
- **First-run PDF parser brittleness.** ~28 monthly PDFs to parse first time. Expect ~5-10 to fail → Sonnet covers most → maybe 2-3 require parser tweaks. Iteration is fast since artifacts are cached.
- **News-sourced indicators.** 5 indicators with `primary.type: "news"` and no URL. v3 uses their `alternate` MoF PDFs instead. Cadence is fiscal-year so first-extraction won't have current values mid-FY.
- **BB index-page redesign.** `pdf_discovery.py` will break if BB changes the publication index page DOM. Mitigation: `valid_range` validation + Discord alert on zero-link discovery.

## 12. Testing

- **Unit:** each parser strategy + the registry + `pdf_discovery` (with fixture HTML/PDF in `tests/fixtures/`).
- **Integration:** end-to-end fetch→parse→snapshot for 1 representative indicator per parser strategy (~7 tests). Uses cached fixtures, no network.
- **`max_client.py`:** mock subprocess (don't actually call Claude in tests). Test parsing of `outer.result`, fence-stripping, error paths.
- **Aggregator:** test the dual-shape output against a fixture set of snapshots covering all 9 domains. Test staleness thresholds per cadence.

Coverage target: >80% for new code per the project standard.

## 13. Out of scope (deferred)

- The Brief migration to ExonVPS.
- Replacing news-sourced indicators with automated extraction.
- Tightening pull-cron `command="rsync"` SSH hardening.
- Removing legacy `bb_forex` / `dse_market` / `commodity_prices` top-level keys from `latest.json`.
- Anomaly threshold global retune (revisit after 2-week soak with real data).

## 14. Decision log

| Decision | Reason |
|---|---|
| Option A (full backlog in one tranche) over phased | User prefers end-to-end validation over incremental |
| Two-stage download-then-parse over single-pass | Network and parse failures isolate; replay from cache |
| Approach 3 hybrid (deterministic + LLM) over pure-LLM or pure-deterministic | Resilience + cost both managed |
| Sonnet 4.6 via Claude Max headless `claude -p` | Mirrors the-brief's pattern; no API cost; sanity-check fits Max quota |
| Run on ExonVPS (Shape A) over Mac-side or manual review | Self-contained; matches the existing scraping architecture; depends on Claude Max device-count check |
| Dual-shape `latest.json` (legacy + domains) for v3 ship | Avoid coupling to The Brief migration |
| Per-indicator `anomaly_threshold` over global `thresholds.json` | Different indicators legitimately move at different scales |

---

*End of design.*
