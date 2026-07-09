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
  upserts that have since been removed.
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
  `policy_rate_sdf` (3-line corridor from BB MEI bulletin, monthly),
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

**Freshness definition.** A metric is fresh when
`as_of >= today − grace(cadence)`. Grace tiers:

| cadence | grace | note |
|---|---|---|
| daily | 2 BD **trading** days | weekend/holiday gap is not stale; the sentinel does the trading-day math, the view approximates with 4 calendar days |
| weekly | 10 days | |
| monthly | 45 days | |
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

### 10.3 SQL package — PREPARED, NOT APPLIED

> **These are DDL/data changes for Adnan's SQL editor only** (no programmatic
> path — the DB is shared with The Brief; `db push` can't reconcile it). Apply in
> order; each block is idempotent. Nothing here has been executed.

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
| `total_revenue_budget_vs_actual` | fiscal | **RETIRE or SOURCE** | no clean single-cell source; budget-vs-actual needs two figures |
| `budget_opex_of_the_fy_vs_utilization` | fiscal | **RETIRE** | no accessible source found; utilisation-vs-budget is not a single scrape |
| `budget_adpex_of_the_fy_vs_utilization` | fiscal | **RETIRE** | same |
| `fx_buy_sale_from_market` | monetary | **SOURCE or RETIRE** | BB FX-intervention figure; confirm a stable BB source cell exists before keeping |
| `nbr_vat_collected_cr` | fiscal | **SOURCE** | brief `nbr_vat_bn` conversion already targets it; wire the NBR component source (media-screen or MFR) or retire the conversion too |
| `nbr_it_collected_cr` | fiscal | **SOURCE** | same |
| `nbr_customs_collected_cr` | fiscal | **SOURCE** | same |
| `ways_means_usage_cr` | monetary | **RETIRE** | BB ways-and-means advances live behind the same walled OMO PDF as the retired `slf_draw_cr` (landmine 24) — likely no HTML route-around |

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

---

**Questions, schema requests, new consumer onboarding**: open an issue
in the EconDelta repo or ping Adnan directly.
