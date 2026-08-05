# EconDelta data contract

**Audience**: a teammate (human or LLM) building a new app that wants to
read Bangladesh economic data without re-implementing scraping. By the
end of this doc you should know enough to write the read path in your
app in 20 minutes.

This is the **stable interface**. Internal scraper details (which
parser handles which PDF, what regex extracts what) live in code; this
file describes what consumers can *depend on*.

> **Contract version 2 — 2026-07-09.** Adds the canonical **freshness &
> vintage contract** and the `v_metric_freshness` surface — see
> [§10 Freshness & vintage contract (E3.1)](#10-freshness--vintage-contract-e31).
> The Brief, YieldScope, and the EconDelta PWA should all read freshness from
> that one view instead of hand-rolling staleness.

---

## 1. What lives where

```
                            ┌──────────────────┐
                            │  EconDelta       │   Producer.
                            │  @ ExonVPS       │   Daily aggregate runs at
                            │  (Dhaka, BDIX)   │   06:10 BDT, fires retries
                            │                  │   at 06:00 / 06:10 if needed.
                            └────────┬─────────┘
                                     │ writes
                                     ↓
                ┌────────────────────────────────────────┐
                │  Supabase metric_history               │  ← THE CONTRACT
                │  (shared `brief` Supabase project)     │
                │                                        │
                │  Read-only for everyone but EconDelta. │
                │  See db/schema.sql for canonical DDL.  │
                └────────┬───────────────────────────────┘
                         │
            ┌────────────┼────────────┬─────────────────┐
            ↓            ↓            ↓                 ↓
       ┌────────┐  ┌──────────┐  ┌─────────┐    ┌────────────┐
       │ Brief  │  │ Mission  │  │ Notifyr │    │ <future    │
       │        │  │ Control  │  │         │    │   app>     │
       └────────┘  └──────────┘  └─────────┘    └────────────┘
                       Read-only consumers — no writes.
```

There is also a **cold local archive** at
`data/archive/<YYYY-MM-DD>.json` on ExonVPS — the full latest.json
serialised once per successful aggregate. This is your fallback if
Supabase ever goes away or needs to be rebuilt; see [§9 Backfill].

## 2. Connecting

### Project URL

The Supabase URL and service-role key live in the brief's environment
file (`/etc/brief.env` on Hetzner). For consumer apps:

- **If you control the host** (VPS, server-side function): use the
  service role key from a managed env file with `chmod 600`. Don't
  hard-code, don't ship in client bundles.
- **If you're a browser app or untrusted consumer**: today there is
  no anon-readable path. Talk to the operator (Adnan) first — they'll
  either issue you a scoped role or expose a server-side proxy.

### Endpoint pattern

PostgREST is exposed at `<SUPABASE_URL>/rest/v1/<table>`. For
`metric_history`:

```
GET <SUPABASE_URL>/rest/v1/metric_history
    ?select=metric_id,as_of,value,source
    &metric_id=eq.<id>
    &order=as_of.desc
    &limit=30

Headers:
    apikey: <SUPABASE_SERVICE_ROLE_KEY>
    Authorization: Bearer <SUPABASE_SERVICE_ROLE_KEY>
```

PostgREST query syntax cheat sheet:

| Filter | Example | Means |
|--------|---------|-------|
| `eq.<v>` | `metric_id=eq.banking_npl_pct` | exact match |
| `in.(a,b)` | `metric_id=in.(banking_npl_pct,banking_car_pct)` | one of |
| `gte.<v>` | `as_of=gte.2026-04-01` | ≥ |
| `lte.<v>` | `as_of=lte.2026-04-30` | ≤ |
| `order` | `order=as_of.desc` | sort |
| `limit` | `limit=30` | cap rows |

## 3. Schema

```sql
CREATE TABLE public.metric_history (
    metric_id    text         NOT NULL,
    as_of        date         NOT NULL,
    value        numeric      NOT NULL,
    source       text         NOT NULL,
    ingested_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_id, as_of)
);
```

Full DDL with indexes, RLS, and column comments lives in
[`db/schema.sql`](../db/schema.sql).

### Field semantics

- **`metric_id`** — Stable identifier. See
  [`indicator-catalog.md`](indicator-catalog.md) for the full list.
  Once an id ships to production it is **never renamed**. To change
  shape (unit, range), introduce a new id, dual-write for a transition
  period, then deprecate the old.
- **`as_of`** — The date the *reading represents*, not the date it was
  scraped. For monthly indicators this is typically the month-end of
  the reporting period (BBS CPI for March → `as_of=2026-03-31` when it
  publishes in mid-April). For daily indicators it's the trading day.
  For quarterly it's the quarter-end. **Always use `as_of` for time-
  series ordering, not `ingested_at`.**
- **`value`** — In the unit declared in `sources-v3.json` for that
  indicator. See unit decoder below.
- **`source`** — Provenance of the row. `EconDelta` is the canonical
  writer (every row from the daily aggregate). Older rows may show
  `BB`, `BBS`, etc. — those came from the brief's transitional inline
  upserts that have since been removed. A small family of ids use a
  **static-seed provenance label** instead: `mof_mfr_static` /
  `mof_mfr_static_provisional` (Ministry of Finance Monthly Fiscal
  Report figures, hand-verified backfill rather than a parse —
  `scripts/backfill_fiscal.py`; the `_provisional` suffix marks
  government bank-borrowing rows that MoF restates between issues, so
  they don't FYTD-reconcile the way the `_static` NBR rows do) and
  `bb_via_press_static` (a one-shot press/parliament-disclosure seed
  for series with no scheduled BB source at all —
  `scripts/seed_npl_structure.py`). These rows are written once, not
  on a recurring pipeline run; `source` is how a consumer tells a
  seeded value apart from one the daily aggregate keeps refreshing.
- **`ingested_at`** — Server-side write timestamp. Diagnostics only;
  consumers should not order by this.

### Unit decoder

The value type per indicator is in the catalog. Decoder:

| Value type | Meaning | Example |
|------------|---------|---------|
| `percent` | Plain percent number | `35.73` for 35.73% |
| `rate` | Generic rate / per-unit price | `133.5` for BDT 133.5/kg |
| `amount_bdt_crore` | BDT in crore (10⁷) | `200486.36` for ~BDT 2.00 trillion |
| `amount_bdt_mn` | BDT in millions | `2004863.6` for the same number expressed in mn |
| `amount_usd_bn` | USD in billions | `34.12` for USD 34.12bn |
| `amount_usd_mn` | USD in millions | `2890` for USD 2.89bn equivalent |
| `ratio` | Plain ratio | `5.16` for money multiplier |
| `count` | Integer count | `123` for #-of-banks |

## 4. Indicator catalog

The full table of every metric_id, with unit / cadence / source / brief
description, lives in [`indicator-catalog.md`](indicator-catalog.md).
That file is **generated** by `scripts/build_catalog.py` from the
authoritative sources (`config/sources-v3.json`, `BRIEF_ALIASES`,
`BRIEF_CONVERSIONS` in `aggregate_latest.py`). Re-run after adding new
indicators:

```bash
cd ~/Projects/clauding-lab/econdelta
python3 scripts/build_catalog.py > docs/indicator-catalog.md
```

Browse-by-section:

- **Forex & reserves** — `bb_gross_reserves`, `usd_bdt_*`,
  `eur_bdt`, `gbp_bdt`, `fx_reserve_gross_and_bpm6`
- **Inflation / macro** — `general_inflation`, `food_inflation`,
  `non_food_inflation`, `private_sector_credit`, plus brief-aliased
  `macro_cpi_headline`, `macro_cpi_food`, `macro_credit_growth`
- **Money market** — `policy_rate_repo`, `policy_rate_slf`,
  `policy_rate_sdf` (3-line corridor from BB's homepage POLICY RATES panel,
  which moves on the MPC announcement; was the BB MEI bulletin until PR #100,
  which lags the announcement by weeks),
  `call_money_rate`,
  `treasury_bill_outstanding` (BDT mn), `treasury_bond_outstanding` (BDT mn),
  `bill_bond_rates` (= 91-day T-Bill yield), `gsec_auction`, plus
  brief-aliased `tbill_outstanding_cr`, `tbond_outstanding_cr`,
  `tbill_91d_yield_pct`, `tbond_tbill_91d`, `gsec_next_auction_cr`
- **Banking** — `broad_money`, `reserve_money`, `money_multiplier`,
  `excess_liquid_asset_total_minimum`, `deposits_of_the_system`, plus
  brief-aliased `banking_*`, plus `gross_npl_ratio` /
  `banking_sector_crar` (FSAR quarterly) / brief-aliased
  `banking_npl_pct` and `banking_car_pct`
- **Government finance** — `tax_revenue`,
  `domestic_borrowing_for_budget_deficit`,
  `foreign_borrowing_for_budget_deficit`,
  `bank_borrowing_for_deficit_financing`, `nsc_outstanding`, plus
  brief-aliased `fiscal_*` and `nbr_fytd_collected_cr` (canonical from
  `tax_revenue` since the news corroborators were retired 2026-05-25)
- **External sector** — `bop_summary`, `categorywise_export`,
  `categorywise_fy_import_breakdown`, `monthly_remittance`,
  `fy_remittance`, `remittance_by_country`, plus brief-aliased
  `remit_monthly_mn`, `remit_fy_mn`
- **Commodities** — `brent_crude_usd_barrel`, `wti_crude_usd_barrel`,
  `gold_usd_oz`, plus 8 DAM retail food prices via brief-aliased
  `dam_*` (rice/atta/egg/chicken/oil/onion/lentil/sugar) and
  EconDelta-native `food_*_bdt`
- **Equities** — DSE summary fields (`dsex`, `dsex_change_pct`,
  `ds30`, `dses`, `turnover_crore`, `advancing`, `declining`)
- **DSE sector heat** — `dse_sector_heat` (Phase 3.1, deferred): a
  `dict[sector_name, pct_avg]` computed daily from constituent moves
  per `config/dse_sector_constituents.json`. Brief renders the 4×2
  heatmap in §06 when this dict is present; until the scraper ships
  the field is absent and the brief gracefully falls back.

## 5. Cadence & freshness

Each indicator has an expected refresh cadence declared in
`sources-v3.json`. Consumers should treat data as stale beyond:

| Cadence | Fresh-by threshold | Example |
|---------|-------------------|---------|
| `daily` | 24 hours | `usd_bdt_*`, food prices |
| `weekly` | 8 days (192h) | `fx_reserve_gross_and_bpm6` |
| `monthly` | 35 days (840h) | `general_inflation`, `monthly_remittance` |
| `quarterly` | 100 days (2400h) | `banking_npl_pct`, `banking_car_pct` |
| `event` | varies — check `sources-v3.json` | `bill_bond_rates` (auctions are biweekly-ish) |
| `fy` | 400 days (9600h) | annual budget figures |

**Non-trading days**: BDT FX, DSE, T-Bill auctions don't update on
Fridays/Saturdays/public holidays. EconDelta will show no new row for
those days. Consumers should display the last available value, not
yesterday's value or zero.

**`as_of` skew**: a row's `as_of` is the *reading date*, not the
ingestion date. A monthly remittance figure for March may not appear
until 3-4 weeks into April with `as_of=2026-03-31`. To detect "we
haven't seen new data in a while", compare `max(as_of)` to today —
not `max(ingested_at)`.

## 6. NULL & missing semantics

There is **no NULL `value`** in `metric_history`. The writer
(`utils/supabase_writer.py:_rows_from_data`) filters out non-numeric
values *before* upsert. Consumer logic for "we don't have this":

```sql
-- "Show me the latest banking_npl_pct, or NULL if we've never seen one"
SELECT value, as_of
FROM metric_history
WHERE metric_id = 'banking_npl_pct'
ORDER BY as_of DESC
LIMIT 1;
-- Empty result set = no data ever.
```

Inside EconDelta itself, an indicator that scrapes badly (parser fails,
returned 0.0 or `needs_review`) is **skipped** by the aggregator — no
row gets written for that day. The next day's successful scrape lands
fresh. If the indicator stays bad for ≥60 days, EconDelta also stops
emitting a stale-fallback to the *current* date in `latest.json`. Net
effect for consumers: gaps in `as_of` history mean the indicator was
unscrapable, not that it was zero.

## 7. Authentication & authorization

### Today

| Role | Read | Write | Used by |
|------|------|-------|---------|
| service_role | yes | yes | EconDelta @ ExonVPS, the brief @ Hetzner |
| anon | no | no | nothing currently |

The service role key is the only credential. It bypasses RLS and has
full DB access — treat it like a root password. **Never embed it in a
client-side bundle.**

### Onboarding a new consumer

If you're standing up a new app that needs read access:

1. **Trusted server-side** (Hetzner, ExonVPS, AWS Lambda, etc.) — copy
   the service role key into a managed env file. Same permissions as
   the brief and EconDelta. This is fine for ops you control.
2. **Untrusted (browser, mobile, public)** — talk to Adnan. We'll mint
   a scoped role and RLS policy specific to your app's needs.
   Generally: read-only, restricted to certain `metric_id` prefixes,
   rate-limited.

## 8. Versioning policy

**The contract is versioned implicitly through the catalog.** Each
indicator_id has a defined unit, cadence, and source. Adding an
indicator is non-breaking. Changing one requires a careful path.

### Adding a new indicator (non-breaking)

1. Add the entry to `config/sources-v3.json` with id, unit, range, cadence.
2. Add the scraper / parser code.
3. Run `scripts/build_catalog.py` to regenerate
   `docs/indicator-catalog.md`.
4. Push. Consumers that don't know about the new id are unaffected;
   those that need it see it on the next aggregate.

### `bb_npl_structure` family (2026-08-04) — a non-config addition

35 ids added outside the normal step-1 path above: sector-wise NPL
distribution from Bangladesh Bank's Financial Stability Report (FSR),
plus band-wise/CMSME NPL detail with no scheduled BB source at all.
Deliberately **not** in `config/sources-v3.json` — see AGENTS.md
landmine 41.

- **ids**: 35 — 22 FSR-written (8 sector rates, 8 sector shares, 4
  sub-sector rates, total advances, gross NPL stock) + 13 seed-only
  (7 band-wise rates, 3 band-wise outstandings, 3 CMSME rates)
- **cadence**: `fiscal_year`
- **sources**: `BB FSR` (annual report, the 22 FSR-written ids) /
  `bb_via_press_static` (one-shot press seed, the 13 seed-only ids —
  see §3 provenance semantics above)
- **extractor path**: `scrapers/bb_npl_structure.py` (LLM extraction
  over a deterministic slice of the FSR PDF, hard arithmetic
  reconciliation gate, all-or-nothing upsert) for the FSR-written ids;
  `scripts/seed_npl_structure.py` (one-shot, dry-run by default) for
  the seed-only ids
- **accepted_stale posture**: all 35 ids sit permanently in
  `sentinel.ACCEPTED_STALE_METRIC_IDS` — FSR is annual with ~6 month
  publication lag, and the band/CMSME series have no scheduled source
  to refresh against at all, so a normal freshness alert would fire
  forever on data that isn't actually broken

### Renaming an indicator (breaking — avoid)

1. **Don't.** Pick a clearer name once, then live with it.
2. If you must: add the new id alongside the old. Have the aggregator
   write *both* for at least 2 weeks (call this dual-write window).
3. Deprecate the old in the catalog with a `DEPRECATED → use <new_id>`
   note.
4. Coordinate with consumers (the brief, Mission Control, etc.). Each
   consumer migrates on its own pace within the dual-write window.
5. After the dual-write window, stop writing the old id. Old historical
   rows stay; just no new writes.

### Changing units

Same as renaming. The unit is part of the contract — `value` field
semantics depend on it. Always introduce a new id; never silently flip
the unit on an existing one.

## 9. Backfill & archive

EconDelta keeps two layers of historical data:

1. **Supabase metric_history** (warm) — the queryable history. Daily
   rows accumulating from the moment Option B shipped (May 2026).
2. **Local archive** at `data/archive/<YYYY-MM-DD>.json` on ExonVPS
   (cold) — the full daily snapshot, exactly what the aggregator
   wrote. Currently retains ~14-30 days; intended as a recovery
   point.

If Supabase ever needs to be rebuilt or migrated:

```bash
# On ExonVPS
cd ~/econdelta
python3 scripts/backfill_supabase.py
```

This walks `data/archive/*.json`, extracts every numeric value from
`.data`, and upserts into `metric_history` with the snapshot's date.
Idempotent on `(metric_id, as_of)`, so safe to re-run.

## 10. Query examples

### Python (any consumer)

```python
import os
import requests

SUPA = os.environ["SUPABASE_URL"].rstrip("/")
KEY  = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

def latest(metric_id: str) -> tuple[float, str] | None:
    r = requests.get(
        f"{SUPA}/rest/v1/metric_history",
        params={
            "select": "value,as_of",
            "metric_id": f"eq.{metric_id}",
            "order": "as_of.desc",
            "limit": "1",
        },
        headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
        timeout=10,
    )
    r.raise_for_status()
    rows = r.json()
    return (rows[0]["value"], rows[0]["as_of"]) if rows else None

print(latest("banking_npl_pct"))   # → (35.73, '2026-05-02')
print(latest("dam_chicken"))        # → (164.5, '2026-05-02')
```

### TypeScript / Node (browser consumers via your own backend)

```ts
import { createClient } from "@supabase/supabase-js";

const supa = createClient(
  process.env.SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,   // server-side only
);

export async function latest(metric_id: string) {
  const { data, error } = await supa
    .from("metric_history")
    .select("value, as_of")
    .eq("metric_id", metric_id)
    .order("as_of", { ascending: false })
    .limit(1)
    .single();
  if (error && error.code !== "PGRST116") throw error;   // not-found is fine
  return data;
}
```

### SQL (analytics)

```sql
-- Last 30 days of NPL ratio + CAR side by side
SELECT
  m.as_of,
  MAX(CASE WHEN metric_id = 'banking_npl_pct' THEN value END) AS npl,
  MAX(CASE WHEN metric_id = 'banking_car_pct' THEN value END) AS car
FROM metric_history m
WHERE metric_id IN ('banking_npl_pct', 'banking_car_pct')
  AND as_of >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY m.as_of
ORDER BY m.as_of DESC;

-- Indicators that haven't refreshed in over 7 days
SELECT metric_id, MAX(as_of) AS last_seen, CURRENT_DATE - MAX(as_of) AS days_old
FROM metric_history
GROUP BY metric_id
HAVING MAX(as_of) < CURRENT_DATE - INTERVAL '7 days'
ORDER BY days_old DESC;
```

## 11. Operational expectations

- **Daily aggregate fires at ~06:10 BDT** on ExonVPS. Retries at
  06:00 and 06:10 if earlier steps flake. Net: by 06:30 BDT every
  scrapeable indicator should have a today-dated row.
- **Sundays / public holidays** still fire — EconDelta runs every
  day. But many sources (BB, DSE) don't publish, so those indicators
  show no new row. That's normal, not a bug.
- **Failure modes you might see**:
  - Network blip during upsert → next aggregate retries (idempotent).
  - Auth key rotated → manual update of `/etc/econdelta.env` on
    ExonVPS + `/etc/brief.env` on Hetzner. No graceful recovery
    today; talk to Adnan.
  - Schema drift between `db/schema.sql` and Supabase reality → run
    the latest migration. Don't apply ad-hoc DDL via the dashboard.
- **Rate limits**: PostgREST on the shared Supabase project has the
  default rate limit. Your read-heavy app should cache locally —
  don't hammer the endpoint per page-view. The brief reads once per
  render and caches in-process.

## 12. Open questions / known limitations

- **No public anon path** — every consumer needs the service role key
  today. Acceptable for trusted server-side apps; blocks public
  dashboards. Future work: scoped roles + RLS.
- **Schema drift risk** — `db/schema.sql` is hand-maintained vs the
  Supabase live state. We don't auto-introspect. Future work:
  CI check that compares.
- **Indicator decomposition gaps** — some brief sections expect
  components (e.g. NBR's VAT/IT/Customs separately) but EconDelta
  currently scrapes the total only. The brief's NBR section will
  show partial / null until decomposition scrapers land.
- **Historical depth** — most indicators have only this month's
  rows in `metric_history` because Supabase write was just shipped
  in May 2026. A few (e.g. `bb_gross_reserves`,
  `tbond_tbill_91d`) have older rows from the brief's transitional
  inline upserts. Use `select min(as_of), max(as_of)` per-indicator
  to know what you can plot.

---

## 10. Freshness & vintage contract (E3.1)

The single rule every consumer must internalise, and the one surface they should
read freshness from.

### 10.1 The canonical rules

**Vintage rule (`as_of`).** `as_of` is the **source's reporting vintage** — the
period the data describes — **never the run date**. It **does not advance until
the source republishes**. A monthly figure last published for May stays at
`as_of = 2026-05-31` every day until BB puts out the June issue. `ORDER BY as_of
DESC LIMIT 1` therefore gives you the **correct data vintage** — that is the
right default for displaying a value.

**Write-liveness rule (`ingested_at`, Option A — owner decision 2026-07-09).**
Because `as_of` legitimately stalls, a value's `as_of` cannot tell you whether
the *pipeline* is still alive. `ingested_at` is POSTED on every upsert (E1.1), so
it advances every run even when `as_of` is pinned. A consumer that needs to know
"is EconDelta still writing this id?" reads **latest-by-`ingested_at`**. **Legacy
daily-stamped rows are NOT deleted** (owner decision) — they are point-in-time
history; the freshness *view* below is the long-term surface that makes the
distinction clean for new consumers.

**2026-08-02 exception (owner-approved).** The no-delete rule above held until
one dated, explicitly-approved exception. On 2026-08-02, with explicit owner
sign-off, the 64 run-date-stamped rows for `gross_reserves_usd_bn` and
`fx_reserve_gross_and_bpm6` with `as_of` in `2026-07-01..2026-08-01` were
**deleted**. Those rows were BB's May figure re-stamped with the run date every
day by the pre-#97 `as_of` forgery (landmine 26/AGENTS.md) — under an
`ORDER BY as_of DESC LIMIT 1` read they outranked the honest month-end
vintages and would have masked BB's real July figure until mid-September. The
`2026-06-30` rows were corrected in place to the true June figure `37.578`. A
full JSON backup of the deleted rows was retained by the operator before
deletion. Rows on or before `2026-06-30` for these two ids, and every other
id's legacy daily-stamped rows, were left untouched — the general no-delete
rule above still stands; this is a one-time, narrowly-scoped exception, not a
new default.

**Freshness definition.** A metric is fresh when
`as_of >= today − grace(cadence)`. Grace tiers:

| cadence | grace | note |
|---|---|---|
| daily | 2 BD **trading** days | weekend/holiday gap is not stale; the sentinel does the trading-day math, the view approximates with 4 calendar days |
| weekly | 10 days | |
| monthly | 45 days | the sentinel and this DB view stay at 45 (unchanged); the briefing's OWN publish gate deliberately uses 60 — see `briefing/freshness.py`'s `_STALE_DAYS_BY_CADENCE` and its worked-timeline comment |
| quarterly | 165 days | |
| fiscal_year | 400 days | |

**Future `as_of` is excluded from "latest".** `debt_gdp_ratio` carries 6 IMF
**projection** rows out to `2031-12-31` (verified 2026-07-09; latest *real*
vintage is `2026-06-05`). Any "latest" read must filter `as_of <= current_date`
or it will read a value from the future.

### 10.2 The surface: `v_metric_freshness`

All three consumers (The Brief, YieldScope, EconDelta PWA) should read freshness
from this **one view** instead of hand-rolling staleness. The freshness sentinel
(E2.1) enforces the same contract on the write side and pages when it breaks.

### 10.3 SQL package — APPLIED (verified live 2026-07-10)

> **✅ APPLIED to the shared prod DB — verified live 2026-07-10** via
> `pg_get_viewdef` (the `v_metric_freshness` definition is byte-identical to Block
> 2), the `grace_days` seeding (Block 1 tiers all present), the deprecation flags
> (Block 3, on every legacy id that has a definition row), the anon-policy set
> (Block 4 — one anon SELECT policy per history table, duplicates gone), and the
> projection split (Block 5 — `debt_gdp_ratio` has 0 future rows,
> `debt_gdp_ratio_proj` holds the 6). Tracked in `supabase/migrations/0012_freshness_contract_e31.sql`.
> These are DDL/data changes for Adnan's SQL editor only (no programmatic path —
> the DB is shared with The Brief; `db push` can't reconcile it). Each block is
> idempotent, so re-running is a safe no-op — but nothing here needs re-applying.

**Block 1 — `grace_days` columns + cadence-seeded defaults:**

```sql
alter table metric_definitions          add column if not exists grace_days integer;
alter table metric_definitions_monthly   add column if not exists grace_days integer;

update metric_definitions set grace_days = case cadence
    when 'daily' then 4        -- 2 trading days + weekend cushion (view is calendar-day)
    when 'weekly' then 10
    when 'monthly' then 45
    when 'quarterly' then 165
    when 'fiscal_year' then 400
    else grace_days end
 where grace_days is null;

update metric_definitions_monthly set grace_days = coalesce(grace_days, 45)
 where grace_days is null;
```

**Block 2 — the `v_metric_freshness` view (over BOTH tables, future-excluded):**

> `metric_definitions_monthly` has **no `cadence` column** (verified live
> 2026-07-09 — its columns are metric_id, display_name, unit, source_url,
> source_attribution, domain, description, notes, timestamps). Every id in the
> monthly system is monthly by construction, so the view infers `'monthly'` from
> the presence of a monthly-definition row — mirroring the sentinel's
> `resolve_cadence` fallback. Do NOT reference `dm.cadence`; it doesn't exist
> and the CREATE VIEW would fail.

```sql
create or replace view v_metric_freshness as
with per_table as (
    select metric_id,
           max(as_of) filter (where as_of <= current_date) as latest_as_of,
           max(ingested_at)                                 as latest_ingested_at
    from metric_history group by metric_id
    union all
    select metric_id,
           max(as_of) filter (where as_of <= current_date),
           max(ingested_at)
    from metric_history_monthly group by metric_id
),
agg as (
    select metric_id,
           max(latest_as_of)       as latest_as_of,
           max(latest_ingested_at) as latest_ingested_at
    from per_table group by metric_id
)
select a.metric_id,
       a.latest_as_of,
       a.latest_ingested_at,
       coalesce(d.cadence,
                case when dm.metric_id is not null then 'monthly' end) as cadence,
       coalesce(d.grace_days, dm.grace_days) as grace_days,
       (current_date - a.latest_as_of)       as age_days,
       (a.latest_as_of >= current_date - coalesce(d.grace_days, dm.grace_days)) as is_fresh
from agg a
left join metric_definitions         d  on d.metric_id  = a.metric_id
left join metric_definitions_monthly dm on dm.metric_id = a.metric_id;

grant select on v_metric_freshness to anon;
```

`grace_days is null` (no definition row) ⇒ `is_fresh` is `null` = "unknown" — it
surfaces the ~100 live metric_ids with no `metric_definitions` row (a real
coverage gap flagged by the PWA work; back-filling those definitions is a
follow-up).

**Block 3 — deprecate/alias the frozen legacy ids** (all verified frozen
2026-07-09 — `ingested_at` stopped in Apr–May and a superseding id is live):

```sql
alter table metric_definitions add column if not exists deprecated boolean default false;
alter table metric_definitions add column if not exists alias_of  text;

update metric_definitions d set deprecated = true, alias_of = v.alias_of
from (values
    ('dse_dsex_close',                'dsex'),
    ('policy_rate_slf_sdf',           'policy_rate_sdf'),   -- superseded by the repo/sdf/slf split (PR #30)
    ('nbr_fytd_collected_tbs',        'tax_revenue'),        -- news scrapers retired (landmine 4)
    ('nbr_fytd_collected_dailystar',  'tax_revenue'),
    ('bb_gross_reserves',             'gross_reserves_usd_bn'),
    ('comm_lng_jkm',                  'lng_price_usd_mmbtu')
) as v(metric_id, alias_of)
where d.metric_id = v.metric_id;
```

Consumers then filter `where not deprecated`.

**Block 4 — drop the duplicate anon policies** (each table carries two identical
anon SELECT policies; keep the canonically-named one).

> **MANDATORY pre-check — run this immediately before Block 4, at execution
> time.** The policy names below are a 2026-07-09 snapshot. If a policy has been
> renamed or one duplicate already removed since, dropping the wrong name could
> silently remove the ONLY working anon-read path and break every consumer read.
> Re-verify, and only drop a policy you can see is one of TWO anon SELECT
> policies on the same table:

```sql
-- Pre-check: expect exactly two anon SELECT policies per history table.
select tablename, policyname, roles::text, cmd
  from pg_policies
 where tablename in ('metric_history','metric_history_monthly')
 order by tablename, policyname;
```

```sql
drop policy if exists "anon read history"                  on metric_history;
drop policy if exists "anon read metric_history_monthly"   on metric_history_monthly;
```

```sql
-- Post-check: each table must STILL have one anon SELECT policy.
select tablename, count(*) as anon_select_policies
  from pg_policies
 where tablename in ('metric_history','metric_history_monthly')
   and roles::text like '%anon%' and cmd = 'SELECT'
 group by tablename;   -- expect 1 and 1
```

**Block 5 (optional) — split the IMF projections off `debt_gdp_ratio`** so no
"latest" read can ever touch a future vintage (the view already filters them, so
this is cleanliness, not correctness):

```sql
update metric_history set metric_id = 'debt_gdp_ratio_proj'
 where metric_id = 'debt_gdp_ratio' and as_of > current_date;
```

**Verification (run after applying):**

```sql
select cadence, count(*), min(grace_days), max(grace_days)
  from metric_definitions group by cadence;                     -- grace seeded
select * from v_metric_freshness where is_fresh = false
  order by age_days desc limit 30;                              -- current breaches
select metric_id, alias_of from metric_definitions where deprecated;  -- marked
select tablename, policyname, roles::text, cmd from pg_policies       -- policies deduped
  where tablename in ('metric_history','metric_history_monthly','auction_calendar','auction_results')
  order by tablename, policyname;
```

### 10.4 pg_policies — live state (verified 2026-07-09)

| table | anon SELECT policies | note |
|---|---|---|
| `metric_history` | `anon_read_metric_history` **+** `anon read history` | anon-readable; DUPLICATE → Block 4 |
| `metric_history_monthly` | `anon_read_metric_history_monthly` **+** `anon read metric_history_monthly` | anon-readable; DUPLICATE → Block 4 |
| `auction_calendar` | `anon read auction_calendar` (+ `service_role_all`) | anon-readable |
| `auction_results` | `anon read auction_results` (+ `service_role_all`) | anon-readable |

All four consumer tables are anon-readable — **AGENTS.md landmine 18's "daily
metric_history has no anon-read" is superseded** (updated). run_logs and other
ops tables remain service-role-only.

### 10.5 Zero-row config ids — retire-or-source decision table

12 `config/sources-v3.json` ids have **never produced a `metric_history` row**
(re-confirmed 2026-07-09 — all 12 return 0 rows). Retiring >1 config id is a
sign-off item (VISION.md) — this is the **decision table, presented not acted**:

| metric_id | domain | recommendation | rationale |
|---|---|---|---|
| `non_nbr_tax_revenue` | fiscal | **SOURCE** | still has a literal `TODO_VPS_FILL_FY26_NON_NBR_BUDGET_CRORE` anchor in its `task`; finish the MFR Table-4 anchor like the fiscal backfill, or retire |
| `non_tax_revenue` | fiscal | **SOURCE** | MoF MFR Table-4 row; same anchor pattern as the working fiscal metrics |
| `tax_gdp_ratio` | fiscal | **DERIVE** | = `tax_revenue` / GDP; mint in aggregate like crr/slr utilisation rather than scrape |
| `rev_gdp_ratio` | fiscal | **DERIVE** | = total revenue / GDP; same |
| `total_revenue_budget_vs_actual` | fiscal | **RETIRED (2026-07-10, owner sign-off)** | no clean single-cell source; budget-vs-actual needs two figures |
| `budget_opex_of_the_fy_vs_utilization` | fiscal | **RETIRED (2026-07-10, owner sign-off)** | no accessible source found; utilisation-vs-budget is not a single scrape |
| `budget_adpex_of_the_fy_vs_utilization` | fiscal | **RETIRED (2026-07-10, owner sign-off)** | same |
| `fx_buy_sale_from_market` | monetary | **RETIRED (2026-07-10, owner sign-off)** | BB FX-intervention figure; confirm a stable BB source cell exists before keeping |
| `nbr_vat_collected_cr` | fiscal | **SOURCE** | brief `nbr_vat_bn` conversion already targets it; wire the NBR component source (media-screen or MFR) or retire the conversion too |
| `nbr_it_collected_cr` | fiscal | **SOURCE** | same |
| `nbr_customs_collected_cr` | fiscal | **SOURCE** | same |
| `ways_means_usage_cr` | monetary | **RETIRED (2026-07-10, owner sign-off)** | BB ways-and-means advances live behind the same walled OMO PDF as the retired `slf_draw_cr` (landmine 24) — likely no HTML route-around |

The 5 rows above are retired and removed from `config/sources-v3.json`. The
remaining 7 (`tax_gdp_ratio`, `rev_gdp_ratio`, `non_tax_revenue`,
`non_nbr_tax_revenue`, `nbr_vat_collected_cr`, `nbr_it_collected_cr`,
`nbr_customs_collected_cr`) are DEFERRED as separate follow-up tasks — 2 DERIVE
+ 5 SOURCE, unchanged in config pending that work.

### 10.6 Legacy-id dedupe decision table (Block 3 targets)

| legacy id | rows | last as_of | last ingested_at | superseded by |
|---|---|---|---|---|
| `dse_dsex_close` | 34 | 2026-04-21 | 2026-04-25 | `dsex` |
| `policy_rate_slf_sdf` | 27 | 2026-05-28 | 2026-05-28 | `policy_rate_sdf` / `_slf` (PR #30) |
| `nbr_fytd_collected_tbs` | 24 | 2026-05-25 | 2026-05-25 | `tax_revenue` (landmine 4) |
| `nbr_fytd_collected_dailystar` | 24 | 2026-05-25 | 2026-05-25 | `tax_revenue` |
| `bb_gross_reserves` | 1 | 2026-03-01 | 2026-04-25 | `gross_reserves_usd_bn` |
| `comm_lng_jkm` | 12 | 2026-04-20 | 2026-04-25 | `lng_price_usd_mmbtu` |

All frozen (`ingested_at` stopped weeks ago) with a live successor — safe to mark
`deprecated` (Block 3). Rows are kept, not pruned (owner decision Option A).

### 10.7 Duplicate alias metric_id pairs — never double-count (D5 reserves-memo)

**Duplicate alias metric_ids — these are the same measurement written twice,
never independent confirmation.** Several `metric_id`s in `metric_history` are
aliases minted from a single upstream value by the aggregator's force-overwrite
alias block (`aggregate_latest.py`'s `main()`, the block that mints
`usd_bdt_exchange_rate` / `fx_reserve_gross_and_bpm6` from `forex.rates` /
`forex.reserves` — see also `_build_source_as_of_map`'s equivalent date
propagation). They are byte-identical, including `as_of`, `ingested_at` and
`source`. When two of them agree, that is arithmetic, not corroboration —
**never** treat an alias pair as two sources confirming each other, and never
let both members enter an average, a count of "sources agreeing", or a
confidence score. Verified 2026-08-05 against prod Supabase (`plans/memos/
reserves-memo-2026-08-05.md`, §5):

| Pair | Rows | Status |
|---|---|---|
| `fx_reserve_gross_and_bpm6` ≡ `gross_reserves_usd_bn` | 58 | Fully identical (all columns) |
| `banking_npl_pct` ≡ `gross_npl_ratio` | 2 | Fully identical (all columns) |
| `banking_sector_crar` ≡ `banking_car_pct` | 1 | Fully identical (all columns) |

The first pair is the one this PR's D5 reserves split addresses directly: the
`_monthly`-suffixed pair the chart contract actually reads
(`gross_reserves_usd_bn_monthly` / `net_reserves_bpm6_usd_bn_monthly`, written
by `aggregate_latest._write_reserves_monthly_split`) is a genuinely NEW pair of
series (gross vs BPM6 — two different accounting bases), not a duplicate of
each other. The daily `fx_reserve_gross_and_bpm6` ≡ `gross_reserves_usd_bn`
alias above is untouched by this PR — collapsing it is a separate,
destructive-restatement decision explicitly deferred to the owner (see the
memo §4.3 for why: 57 of 58 rows carry a run-date `as_of` forgery predating
PR #97, so a naive collapse would also need the `as_of` restatement, which
needs its own sign-off).

**A fourth pair looked the same shape but is NOT a clean alias — flag, don't
assume benign:**

| Pair | Rows | Status |
|---|---|---|
| `banking_reserve_money` ≡ `reserve_money` | 94 | Identical on 93/94 dates — **diverges at 2026-07-10** |

On 2026-07-10, `reserve_money` = 435,407.1 while `banking_reserve_money` =
485,542.3 (a 50,135.2 gap, ~11.5%), with all 93 other dates in exact agreement.
A single divergent day in an otherwise byte-identical pair is the signature of
one writer catching a bad or partial read on that one date — it needs its own
investigation (which of the two writers misread; whether the divergent date
should be corrected or the pair should stay independent going forward) and is
explicitly **out of scope for this PR** — tracked as an open follow-up, not
fixed here.

**A fifth pair is the OPPOSITE shape of the two above — diverges most of the
time, converges only on the newest row. Not a confirmed alias; not cleanly
independent either.** Found during config-conversion batch 2 (issue #113,
2026-08-05) while checking whether `general_inflation` and
`point_to_point_inflation` are a duplicate pair worth collapsing:

| `as_of` | `general_inflation` | `point_to_point_inflation` | Agree? |
|---|---:|---:|---|
| 2026-06-30 | 9.16 | 9.16 | yes |
| 2026-06-05 | 8.59 | 9.04 | no (0.45pp gap) |
| 2026-06-01 | 8.60 | 8.71 | no (0.11pp gap) |
| 2026-05-31 | 8.63 | 9.42 | no (0.79pp gap) |
| 2026-05-30 | 8.60 | 8.71 | no (0.11pp gap) |

Both ids are LLM-only extractions from the same BB MEI PDF table ("A.
Consumer price index (CPI) and rate of inflation at national level"), which
prints TWO methodology groups side by side — "Twelve-month average" and
"Point to Point" — each with its own General/Food/Non-food columns. Checked
against the real June-2026 fixture (`tests/_pdfs/bb_mei_2026_june.pdf`).
`general_inflation`'s config task hint is `"page 15"` — a 1-indexed PDF-page
number, whose ±3-page search window (`_extract_pdf_text`'s default) covers
PDF pages 12–18. The actual CPI table sits at PDF page 17 (printed page 14
per the document's own footer — inside that window, so this IS the table
`general_inflation`'s config already points at): the "Twelve-month average →
General" column reads 8.59 (Apr) / 8.63 (May) / 8.68 (Jun) — matching
`general_inflation`'s historical values almost exactly — while the "Point to
Point → General" column reads 9.04 (Apr) / 9.42 (May) / 9.16 (Jun) — matching
`point_to_point_inflation`'s. `food_inflation`/`non_food_inflation` (same
config task template, "page 15, first table") reliably resolve to the
Point-to-Point Food/Non-food columns across the same history — only
`general_inflation` has been drifting onto the WRONG methodology group. Most
likely read: `general_inflation`'s task ("Go to page 15 of the doc, first
table") gives the LLM no methodology-group qualifier, so it has been
inconsistently picking between the two "General" columns run to run — this
converged with `point_to_point_inflation` for the first time on 2026-06-30
only because that row came from config-conversion batch 1's NEW deterministic
`pdf_component` extraction (reads the Executive Summary's explicit "Headline
inflation (p-t-p) ... 9.16 percent" sentence, unambiguous by construction),
not because the two ids became a stable pair.

**Verdict: not a confirmed duplicate (values disagree on 4 of the last 5
dates) and not safely independent either (the disagreement looks like one
metric's extraction being unreliable, not two real different numbers) —
flagged, not fixed.** Do not add this pair to the confirmed-alias table
above. Do not convert `general_inflation` to a deterministic parser until the
underlying dynamic-month-row table problem (same blocker as
`food_inflation`/`non_food_inflation`, AGENTS.md landmine 49) is solved.

**Which column to anchor `general_inflation` to once a dynamic-row parser
exists is an OWNER decision, not an engineering one — the two options have
materially different consequences, not just different numbers:**

- **Option A — anchor to "Point to Point → General"** (matching
  `point_to_point_inflation`'s own extraction). Consequence: `general_inflation`
  becomes a genuine, permanent duplicate of `point_to_point_inflation` — which
  ALREADY reads exactly this number (the Executive Summary's own "9.16"
  sentence IS the Point-to-Point General figure for June) — inside the very
  section of this document whose purpose is "never double-count these as
  independent confirmation." It would also BREAK `general_inflation`'s own
  series continuity: its production history (8.59 / 8.60 / 8.63) tracked the
  Twelve-month-average column, not Point-to-Point, and there is no backfill
  plan to reconcile the two.
- **Option B — anchor to "Twelve-month average → General"** (matching
  `general_inflation`'s own historical values). Consequence: this is a real,
  separately-published BB concept — the MEI's own Executive Summary states it
  independently ("12-month average inflation increased to 8.68 percent in
  June 2026") — so the pair stays legitimately independent, and the metric's
  own history stays CONTINUOUS (no discontinuity, no backfill needed).

Whichever option the owner picks, a THIRD candidate in the same ±3-page
window must be ruled out first: the MEI's Wage Rate Index table ("B. Wage
Rate Index (WRI) and growth rate at national level", PDF page 18 / printed
page 15) ALSO has its own "Point to Point → General" column (June growth
8.18) — a plausible-looking wrong match for anyone building a row/column
selector against a bare `"General"` anchor without first confirming which
TABLE it's scoped to. Per the domain-expert-outranks-converging-AIs rule,
this PR does not pick an option — it hands off the anchor, the consequences,
and the third-candidate trap, ready for a sign-off. See also
`AGENT_LEARNINGS.md` (2026-08-05 entry) for the incident writeup.

---

**Questions, schema requests, new consumer onboarding**: open an issue
in the EconDelta repo or ping Adnan directly.
