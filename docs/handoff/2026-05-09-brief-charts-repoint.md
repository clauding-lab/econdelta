# Handoff: Repoint The Brief charts from legacy `tb_*` tables → `metric_history`

**Date:** 2026-05-09
**Source session:** EconDelta investigation triggered by stale Brief charts
**Target session:** the-brief repo (parallel session)
**Scope:** Brief-side fix only. EconDelta requires no code change for DSEX / Brent / yields. One genuinely-missing series (LNG JKM) is flagged separately.

---

## TL;DR

The Brief's `chart_series_fetcher.py` reads from four legacy chart tables (`tb_brent_daily`, `tb_dsex_daily`, `tb_lng_jkm_weekly`, `tb_yield_curve`). Those tables are frozen — their writer (`the-brief/ingest.py` + `.github/workflows/daily-update.yml`) was deleted in commit `2317436` (V6 cutover, 2026-05-04). EconDelta never wrote them and doesn't need to.

The same data EconDelta scrapes daily lives in `metric_history` under different IDs. Repoint the four fetchers — three trivially, one needs a new scraper.

---

## Repoint mapping (Supabase project `ssbliukchgibjcjohibi`)

| Brief fetcher | Legacy table | New source in `metric_history` | Latest as_of | Status |
|---|---|---|---|---|
| Brent (chart) | `tb_brent_daily` | `metric_id = 'brent_crude_usd_barrel'` | **2026-05-09** | ✅ Repoint |
| DSEX (chart) | `tb_dsex_daily` | `metric_id = 'dsex'` (NOT `dse_dsex_close` — legacy) | **2026-05-07** (Thu; Fri+Sat non-trading) | ✅ Repoint |
| Yield curve (chart) | `tb_yield_curve` | `metric_id IN ('tbill_91d_yield_pct','tbill_182d_yield','tbill_364d_yield','tbond_bond_5y','tbond_bond_10y')` | **2026-05-09** | ✅ Repoint + reshape |
| LNG JKM (chart) | `tb_lng_jkm_weekly` | `metric_id = 'comm_lng_jkm'` | **2026-04-20** ⚠ STALE 19d | ❌ No active scraper anywhere |

`metric_history` schema: `(metric_id text, as_of date, value jsonb, source text, ingested_at timestamptz)`. No `source_as_of` column in production despite the May 4 plumbing — apparently never deployed.

For Brent / DSEX / bonds: shape change in the fetcher is straightforward — query becomes `?metric_id=eq.<id>&as_of=gte.<since>&select=as_of,value&order=as_of.asc`, then unwrap `value` (stored as jsonb scalar — e.g. `100.49` — needs a `float(row["value"])` in Python).

For yield curve: today's `tb_yield_curve` shape is `(as_of, tenor, yield_pct)` — multiple tenors per as_of. `metric_history` has one row per (metric_id, as_of), so the fetcher must group by as_of after fetching the 5 tenor metric_ids and pivot into the curve shape the chart consumes. Existing yield-curve chart only renders 5y/10y/20y/2y per the May 9 brief audit; we don't currently have 2y or 20y in metric_history (only 91d/182d/364d t-bills + 5y/10y t-bonds). Either trim the chart to 91d→364d→5y→10y, or add 2y/20y scrapers (separate work).

---

## LNG JKM — the only genuine gap

`comm_lng_jkm` is stale because no scraper writes it. The May 4 session notes mention LNG JKM as part of the brief but EconDelta's `commodity_prices.py` only scrapes Brent / WTI / Gold from yfinance. JKM is a Platts assessment with no free public API — same problem the deleted `ingest.py` had ("DSEX / LNG / T-Bills / Yield curve: no free public API").

Three options for LNG, in order of effort:
1. **Drop the LNG chart from the Brief** until a source is wired (cheapest — matches the V6 design intent of "render gracefully when missing").
2. **Manual weekly entry** via Supabase Studio (one row/week, ~30 sec), matching how DSEX/yield were "manually updated OR Claude-generated" historically per `b40631c:ingest.py` docstring.
3. **Add `lng_jkm` ticker to commodity_prices.py** using a yfinance proxy (e.g. `NG=F` Henry Hub, but that's US gas not Asian LNG — not equivalent). Or scrape from EIA / Reuters article scrape — half-day work, brittle.

Recommend option 1 or 2 for now. If option 2: writes go directly to `metric_history` with `metric_id='comm_lng_jkm', source='manual'`.

---

## Change surface in the-brief repo

```
brief/chart_series_fetcher.py   # 4 fetcher functions, lines ~150–270
scripts/backfill_metric_history.py  # docstring lines 14–21 are stale on
                                     # metric_id names — fix or delete the script
                                     # (it's a one-shot legacy migrator)
tests/test_chart_series_fetcher*.py # mock fixtures change shape
```

The companion change `lib/chartConfigs.ts` (pointRadius>0 on Brent/LNG date markers, FX flows x-axis labels) was already flagged out-of-scope and remains so.

---

## Out-of-scope EconDelta operational issues found in this session

1. **`econdelta-parse.service` failing** — Claude API returns HTTP 401 "Invalid authentication credentials" on every preflight. The credentials in `/etc/econdelta.env` (or wherever `parse_all.py` reads them) are expired/invalid. Parse stage has been dead long enough that the log is full of identical 401s.
2. **`econdelta-forex.timer` + retry inactive since 2026-05-05** — matches the unsolved BB Akamai blockade flagged as item 2B in the 2026-05-04 session.
3. **`source_as_of` column missing from production `metric_history`** despite the May 4 schema work shipping. The migration was never applied to `ssbliukchgibjcjohibi`. Aggregate writes proceed without it (column was opportunistic). Re-confirm before re-deploying any `as_of`-arch work.

These are flagged for triage, not part of this handoff.

---

## What "no Brief-side change" was supposed to mean

The original brief's framing — "fix EconDelta upstream and the next Brief auto-fire picks up fresh data with zero Brief-side change" — only works if EconDelta is the upstream. It isn't, and never was, for the four chart tables. That framing was based on a wrong model of the data flow and should be updated.
