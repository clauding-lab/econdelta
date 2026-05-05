---
date: 2026-05-05
project: EconDelta
topic: /macro tab — long-horizon analytical view
status: design-approved
authors: Adnan Rashid (with Claude)
---

# `/macro` — Long-Horizon Analytical View

## Summary

Add a new top-level tab `/macro` to the EconDelta PWA that reproduces Macro
Observer's (`https://macro.thenazmussakib.com/`) long-horizon chart design
language using monthly time-series data going back to **January 2012**. The
new tab is read-only against a new Supabase table `metric_history_monthly`
that is seeded once from Macro Observer's public `macro_monthly_data.json`.

The existing `/archive` tab (90-day operational pipeline view) remains
untouched. The two views serve different audiences: `/archive` answers "is
the pipeline healthy today?" and `/macro` answers "what does Bangladesh's
macro trajectory look like over 14 years?".

## Goals

- Reproduce Macro Observer's **13 charts** with high visual fidelity using
  the same charting library (Chart.js 4.4.0) so the reproduction matches
  the source naturally.
- Mine and store **as much historical monthly data as possible** in
  Supabase under a new `metric_history_monthly` table so EconDelta becomes
  the durable system of record going forward.
- Reproduce the **events layer in graph-only form** — 11 event cards, each
  with a click-to-open modal containing 5 KPI snapshots and 2 mini-charts
  windowed ±6 months around the event date. Drop the prose narrative.
- Annotate the DSEX chart with **11 colored event dots** that share data
  with the events strip below.
- Keep the existing `/archive`, `/dashboard`, daily scrapers, and parsers
  fully unchanged. The new tab is fully isolated.

## Non-Goals

- The **Macro Temperature composite gauge** (43.5/100 hero score) is
  excluded. Out of scope.
- The **"Where We Are vs Where We've Been" driver cards** (10 cards with
  secular vs cyclical metrics) are excluded. Out of scope.
- **Editorial NOTE blocks** under each chart (analyst commentary with
  bolded numbers) are excluded.
- **Magazine masthead** (Vol/Issue, dateline, author photo) is excluded;
  EconDelta keeps its own header.
- **Event narrative paragraphs** (~150 words per event) are excluded.
  Modals show only KPI rows + mini-charts.
- Migration to **primary sources** (BB Statistical Database, BBS, DSE
  monthly archives) is deferred. Macro Observer JSON is the seed; primary
  sources will be revisited only if data inconsistencies surface.
- **Daily-and-monthly unified time-series views** are out of scope for v1.
  Keeping the two granularities in separate tables; unification can come
  in phases once EconDelta has accumulated enough daily data to be
  meaningful at long horizons.

## Decisions Log

Decisions taken during brainstorming, kept here so reviewers can see the
trade-offs.

### D1 — Information architecture: new tab, not replacement

**Decision:** Add a new top-level tab `/macro`, leave `/archive` untouched.

**Why:** The two views serve different mental models — operational
pipeline health (`/archive`) vs analytical long-horizon (`/macro`).
Mixing them in a single tab dilutes both. The cost of one extra nav slot
is negligible.

**Rejected:** (a) replacing `/archive` outright would lose run-health
visibility currently used to spot scrape failures; (b) sub-view toggle
inside `/archive` adds cognitive overhead with no offsetting benefit.

### D2 — Data sourcing: Macro Observer JSON as seed

**Decision:** Seed `metric_history_monthly` from Macro Observer's public
`macro_monthly_data.json`. Attribute Nazmus Sakib + BB/BBS/DSE in the
`source` and `source_attribution` columns.

**Why:** Ships in ~1 day vs 1–2 weeks for primary-source extraction. The
user wants to see the visual product first, then validate or replace
metric-by-metric only if inconsistencies surface.

**Rejected:** (b) primary-source extraction from BB Statistical Database,
BBS CPI bulletins, and DSE monthly archives — deferred to future phase;
(c) hybrid (seed + replace) — the "replace" half is deferred indefinitely
and will be triggered by data-quality observations, not by schedule.

### D3 — Chart scope: full parity minus analytical layer

**Decision:** Reproduce all **13 charts** plus the **events layer
(graph-only)**. Exclude the Macro Temperature gauge, the 10 driver cards,
and the editorial NOTE blocks.

**Why:** User stated explicit interest in "data and the graph style"
only; excluded the analytical commentary layers.

### D4 — Schema: separate `metric_history_monthly` table

**Decision:** New table `metric_history_monthly` mirroring the
`metric_history` schema. New table `metric_definitions_monthly` for
attribution + units + domain mapping.

**Why:** No migration risk to the existing operational `metric_history`.
Clean semantic separation between daily scrapes and monthly historical
publications. Easier future migration to primary sources because the
monthly destination is untangled from the daily flow.

**Rejected:** Single `metric_history` table with a `granularity` column —
adds risk to existing system, every existing query has to learn to filter
granularity.

### D5 — Visual styling: Macro Observer chart styling inside canvas only

**Decision:** Charts render with Macro Observer's exact in-canvas styling
(Fraunces fonts via Chart.js font config, oxblood `#c8472b` primary line
color, ghost prior-month opacity, FIG.NN labels, etc.). EconDelta's nav
and page chrome unchanged.

**Why:** User said "copy the design language of graphs". That's
chart-level. Page-level treatment was not requested. EconDelta's existing
header and palette already share the cream/warm-tone DNA so the result
will not feel inconsistent.

**Rejected:** (a) EconDelta fonts/palette inside charts too — would not
match the source visually; (c) full magazine treatment (Fraunces +
`#F4F1EA` bg + oxblood accent on the whole `/macro` body) — adds asset
loading and a CSS scope toggle for a benefit the user did not request.

### D6 — Implementation: Chart.js 4.4.0, lazy-loaded

**Decision:** Use Chart.js 4.4.0 (same version Macro Observer uses),
loaded lazily from CDN on first `/macro` visit and cached by the service
worker.

**Why:** Same library produces the same output. Half the LOC vs the
custom-SVG alternative (~700–1,000 vs 1,500–2,500). 200 KB cost is paid
once, only on `/macro` visits, and is cached. Existing `/dashboard` and
`/archive` stay zero-dependency.

**Rejected:** (b) extending the custom SVG `TrendChart` for 13 chart
types — significant per-type engineering, lower fidelity, more LOC; (c)
hybrid (SVG for simple, Chart.js for complex) — two paradigms in one
page is hard to maintain.

## Architecture

Three new pieces. No changes to existing code paths.

```
┌──────────────────────────────────────────────────────────────────────┐
│  ONE-SHOT INGESTION  (Python, run locally or on VPS)                 │
│                                                                      │
│  scripts/seed_macro_monthly.py                                       │
│    1. fetch https://macro.thenazmussakib.com/macro_monthly_data.json │
│    2. transform via KEY_MAP: their key → EconDelta metric_id         │
│    3. UPSERT into metric_history_monthly                             │
│    4. populate metric_definitions_monthly with attribution + units   │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  SUPABASE                                                            │
│                                                                      │
│  (new) metric_history_monthly      ← seeded by script                │
│  (new) metric_definitions_monthly  ← attribution + units             │
│  metric_history                    (existing, untouched)             │
│  metric_definitions                (existing, untouched)             │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  PWA  (browser)                                                      │
│                                                                      │
│  pwa/pages/macro.jsx                                                 │
│    • lazy-loads Chart.js 4.4.0 from CDN on first /macro visit        │
│    • queries metric_history_monthly via supabase-client.js           │
│    • renders 13 Chart.js canvases + 11 event modals                  │
└──────────────────────────────────────────────────────────────────────┘
```

## Data Flow

| Step | Component | Output |
|---|---|---|
| 1 | `scripts/seed_macro_monthly.py` reads Macro Observer JSON | 13 charts decompose to ~28 distinct `metric_id`s (some charts carry multiple series, e.g. CPI general/food/non-food, yield curve at 5 tenors) × ~170 months Jan'12–latest ≈ **~4,760 rows** |
| 2 | Script transforms keys via `KEY_MAP` and UPSERTs | Populated `metric_history_monthly` |
| 3 | Browser visits `/macro`; `supabase-client.js` fetches `metric_history_monthly` for the in-scope metric IDs on page mount | `window.ED_DATA.macroMonthly` map keyed by `metric_id` |
| 4 | `pwa/pages/macro.jsx` reads from that map, hands datasets to per-chart Chart.js config functions | 13 Chart.js canvases rendered |
| 5 | User clicks an event card → modal opens → reads same monthly data, slices ±6m around the event date, renders 2 mini-charts and 5 KPI rows | Modal with KPIs + 2 mini-charts |

**Key property:** the page is read-only against Supabase and self-contained.
No new write paths from the PWA. No new scrapers.

## Schema

### `metric_history_monthly`

```sql
create table metric_history_monthly (
  id              bigserial primary key,
  metric_id       text not null,
  as_of           date not null,         -- always day 1 of month, e.g. 2024-03-01
  value           numeric not null,
  source          text not null,         -- 'macro_observer_seed' for v1
  source_as_of    date,                  -- when the source published this datapoint
  ingested_at     timestamptz not null default now(),
  notes           text,
  unique (metric_id, as_of)
);

create index idx_mhm_metric_asof on metric_history_monthly (metric_id, as_of desc);
```

`as_of` is normalised to the first day of the month so January 2024
becomes `2024-01-01`. This avoids ambiguity between mid-month vs
end-of-month source publication conventions.

### `metric_definitions_monthly`

```sql
create table metric_definitions_monthly (
  metric_id           text primary key,
  display_name        text not null,
  unit                text not null,         -- '%', 'BDT bn', 'USD bn', 'index', 'mo'
  source_url          text,
  source_attribution  text,                  -- 'Nazmus Sakib · BB · BBS · DSE'
  domain              text not null,         -- see DOMAIN_VALUES below
  description         text,
  notes               text
);
```

`domain` enum values (text, validated in app code):

- `prices_policy` — CPI, repo, T-bill, yield curve, real rate
- `credit_money` — domestic credit components, M1/M2
- `external` — trade, remittance, reserves, import cover, BDT/USD, REER
- `capital_market` — DSEX index, DSEX turnover

These map 1:1 to the four section dividers in `/macro`.

### Migration files

- `supabase/migrations/023_metric_history_monthly.sql` — table +
  index above
- `supabase/migrations/024_metric_definitions_monthly.sql` — table
  above

Existing `metric_history` and `metric_definitions` are not touched.

## Frontend Components

`pwa/pages/macro.jsx` is the page entry. The composition tree:

```
PageMacro (root)
├── PageHead
│   kicker  : "Long-horizon · monthly observations"
│   title   : "Macro"
│   meta    : "JAN 2012 — <latest> · 13 charts · 11 events"
│
├── Section ("Prices & Policy", 5 charts)
│   ├── ChartCard fig=4   "CPI Inflation · Point-to-Point"
│   ├── ChartCard fig=5   "Inflation · 12-Month Average"
│   ├── ChartCard fig=6   "Repo & 364-Day T-Bill"
│   ├── ChartCard fig=7   "Sovereign Yield Curve · 1Y to 20Y"
│   └── ChartCard fig=8   "Real Policy Rate"
│
├── Section ("Credit & Money", 3 charts)
│   ├── ChartCard fig=1   "Domestic Credit · Composition"
│   ├── ChartCard fig=2   "Credit Growth · Public, Private, Total"
│   └── ChartCard fig=3   "Money Growth · Narrow (M1) & Broad (M2)"
│
├── Section ("External Sector", 4 charts)
│   ├── ChartCard fig=9   "Foreign Exchange Inflows vs. Outflows"
│   ├── ChartCard fig=10  "FX Reserves"
│   ├── ChartCard fig=11  "Import Cover · Adequacy Gauge"
│   └── ChartCard fig=12  "BDT / USD & Real Effective Exchange Rate"
│
├── Section ("Capital Market", 1 chart, event-dot annotated)
│   └── ChartCard fig=13  "DSEX Index & Daily Turnover"
│
└── EventStrip (11 cards, click → EventModal)
        EventModal: KPIs (5) + Mini-Chart-A + Mini-Chart-B
```

### Component primitives

| Component | Responsibility | Approx. LOC |
|---|---|---|
| `<ChartCard fig label subtitle latestValue series chartConfig />` | Renders one card: FIG.NN · title · subtitle · latest value · canvas. Mounts Chart.js on a ref'd canvas; tears down on unmount. | ~60 |
| `<EventStrip events onSelect />` | Renders 11 event cards in a horizontal/responsive strip; click bubbles `onSelect(event)` | ~30 |
| `<EventModal event monthlyData onClose />` | Renders modal: tag/title/date · 5 KPI rows · 2 mini-canvases. KPIs computed by slicing `monthlyData` at the event date. | ~80 |

### Chart configs

13 chart-config builder functions live in
`pwa/pages/macro/chartConfigs.js` and are pure functions of monthly
data:

```js
// Each function takes the relevant slice of monthly data and returns a
// Chart.js config object. No DOM access, no side effects.

export function cpiP2PConfig(seriesByMetric) { /* ... */ }
export function inflation12mAvgConfig(seriesByMetric) { /* ... */ }
export function repoAndTbillConfig(seriesByMetric) { /* ... */ }
export function yieldCurveConfig(seriesByMetric) {
  // Special: builds N+1 datasets — one per month — with current bold,
  // priors at opacity 0.08, x-axis = tenor (1Y, 2Y, 5Y, 10Y, 20Y).
}
export function realPolicyRateConfig(seriesByMetric) { /* diverging bars */ }
export function domesticCreditCompositionConfig(seriesByMetric) { /* stacked */ }
export function domesticCreditGrowthConfig(seriesByMetric) { /* multi-line */ }
export function moneyGrowthConfig(seriesByMetric) { /* M1 + M2 */ }
export function fxFlowsConfig(seriesByMetric) { /* symmetric stacked bars */ }
export function fxReservesConfig(seriesByMetric) { /* gradient area */ }
export function importCoverConfig(seriesByMetric) { /* with threshold zones */ }
export function bdtUsdReerConfig(seriesByMetric) { /* dual-axis */ }
export function dsexConfig(seriesByMetric, events) { /* event-dot annotated */ }
```

Two more for event modals:

```js
export function eventInflationRepoMiniConfig(seriesByMetric, eventDate) { /* ±6m */ }
export function eventReservesBdtMiniConfig(seriesByMetric, eventDate) { /* ±6m */ }
```

### Lazy load

```js
async function ensureChartJS() {
  if (window.Chart) return window.Chart;
  await import(
    'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js'
  );
  return window.Chart;
}
```

Called once when `/macro` mounts; subsequent renders reuse `window.Chart`.
Service worker (`pwa/sw.js`) is updated to cache the Chart.js URL on first
fetch so offline navigation to `/macro` continues to work.

### Routing & nav

- `pwa/index.html` — add `<script src="pages/macro.jsx?v=...">`
- `pwa/components.jsx` (nav array) — add `{ href:'#/macro', label:'Macro', badge:'14y', icon:'≋' }` after the existing 5 entries
- `pwa/index.html` route table — add `if(route === '/macro'){ Page = PageMacro; label = '0X Macro'; }`
- `pwa/sw.js` cache list — add `'./pages/macro.jsx'` and `'./pages/macro/chartConfigs.js'`

## Ingestion Script — `scripts/seed_macro_monthly.py`

```
python -m scripts.seed_macro_monthly --dry-run    # show what would change
python -m scripts.seed_macro_monthly              # execute
python -m scripts.seed_macro_monthly --refresh    # force re-fetch + upsert
```

### Flow

1. `requests.get('https://macro.thenazmussakib.com/macro_monthly_data.json')`
2. Validate top-level keys against `EXPECTED_KEYS` constant; warn on
   missing, error on schema break (no recognised metric).
3. For each `(macro_observer_key, series)`, look up `KEY_MAP[key]`:
   - If mapped to a single `metric_id`, build rows from the
     `(date, value)` pairs.
   - If mapped to a multi-tenor object (e.g. `yield_curve` →
     `tbill_91d_yield_monthly`, `tbill_182d_yield_monthly`,
     `tbill_364d_yield_monthly`, `tbond_5y_yield_monthly`,
     `tbond_10y_yield_monthly`, `tbond_20y_yield_monthly`), explode to
     one row per (tenor, date).
4. Build rows: `{metric_id, as_of=YYYY-MM-01, value, source='macro_observer_seed',
   source_as_of=YYYY-MM-DD, ingested_at=now()}`.
5. Bulk upsert via existing `utils/supabase_writer.py` patterns,
   `on conflict (metric_id, as_of) do update`.
6. Sync `metric_definitions_monthly` from `KEY_MAP` metadata
   (display_name, unit, domain, source_attribution) — upsert as well.
7. Print summary: `4,760 rows upserted across 28 metric_ids covering 13
   charts (multi-series charts contribute multiple metric_ids), oldest
   2012-01-01, latest 2026-04-01`.

### `KEY_MAP` shape

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class MetricMap:
    metric_id: str
    display_name: str
    unit: str           # '%', 'BDT bn', 'USD bn', 'index', 'mo'
    domain: str         # see DOMAIN_VALUES
    notes: str = ""

KEY_MAP: dict[str, MetricMap | dict[str, MetricMap] | None] = {
    'cpi_p2p_general':       MetricMap('point_to_point_inflation_monthly',
                                       'CPI YoY (general)', '%', 'prices_policy'),
    'cpi_p2p_food':          MetricMap('cpi_p2p_food_monthly',
                                       'CPI YoY (food)', '%', 'prices_policy'),
    'cpi_p2p_nonfood':       MetricMap('cpi_p2p_nonfood_monthly',
                                       'CPI YoY (non-food)', '%', 'prices_policy'),
    'cpi_12m_general':       MetricMap('cpi_12m_avg_monthly',
                                       'CPI 12-month average', '%', 'prices_policy'),
    'repo_rate':             MetricMap('bb_repo_rate_monthly',
                                       'BB repo rate', '%', 'prices_policy'),
    'tbill_364d':            MetricMap('tbill_364d_yield_monthly',
                                       '364-day T-bill yield', '%', 'prices_policy'),
    'yield_curve':           {  # multi-tenor explosion
        '1y':  MetricMap('yield_1y_monthly',  '1Y yield',  '%', 'prices_policy'),
        '2y':  MetricMap('yield_2y_monthly',  '2Y yield',  '%', 'prices_policy'),
        '5y':  MetricMap('yield_5y_monthly',  '5Y yield',  '%', 'prices_policy'),
        '10y': MetricMap('yield_10y_monthly', '10Y yield', '%', 'prices_policy'),
        '20y': MetricMap('yield_20y_monthly', '20Y yield', '%', 'prices_policy'),
    },
    'real_policy_rate':      MetricMap('real_policy_rate_monthly',
                                       'Real policy rate', '%', 'prices_policy'),
    'domestic_credit_total': MetricMap('domestic_credit_total_monthly',
                                       'Total domestic credit', 'BDT bn', 'credit_money'),
    'domestic_credit_public': MetricMap('domestic_credit_public_monthly',
                                       'Public-sector domestic credit', 'BDT bn', 'credit_money'),
    'domestic_credit_private': MetricMap('domestic_credit_private_monthly',
                                       'Private-sector domestic credit', 'BDT bn', 'credit_money'),
    'private_credit_growth_yoy': MetricMap('private_credit_growth_yoy_monthly',
                                       'Private credit growth YoY', '%', 'credit_money'),
    'public_credit_growth_yoy': MetricMap('public_credit_growth_yoy_monthly',
                                       'Public credit growth YoY', '%', 'credit_money'),
    'm1_growth_yoy':         MetricMap('m1_growth_yoy_monthly',
                                       'M1 growth YoY', '%', 'credit_money'),
    'm2_growth_yoy':         MetricMap('m2_growth_yoy_monthly',
                                       'M2 growth YoY', '%', 'credit_money'),
    'exports_usd_mn':        MetricMap('exports_usd_mn_monthly',
                                       'Exports', 'USD mn', 'external'),
    'imports_usd_mn':        MetricMap('imports_usd_mn_monthly',
                                       'Imports', 'USD mn', 'external'),
    'remittance_usd_mn':     MetricMap('remittance_usd_mn_monthly',
                                       'Remittance', 'USD mn', 'external'),
    'fx_reserves_gross_bn':  MetricMap('gross_reserves_usd_bn_monthly',
                                       'FX reserves (gross)', 'USD bn', 'external'),
    'import_cover_months':   MetricMap('import_cover_months_monthly',
                                       'Import cover', 'mo', 'external'),
    'bdt_usd':               MetricMap('usd_bdt_mid_monthly',
                                       'BDT / USD', 'BDT', 'external'),
    'reer':                  MetricMap('reer_monthly',
                                       'REER (100 baseline)', 'index', 'external'),
    'dsex':                  MetricMap('dsex_monthly',
                                       'DSEX index', 'index', 'capital_market'),
    'dsex_turnover':         MetricMap('dsex_turnover_monthly',
                                       'DSEX daily turnover', 'BDT mn', 'capital_market'),
}
```

The exact set of source keys will be confirmed during implementation by
inspecting the actual `macro_monthly_data.json` payload before finalising
`KEY_MAP`. Above is the design intent based on what the rendered page
exposes.

### Events seed

Events come from a separate static asset bundled with the PWA, not from
Supabase: `pwa/pages/macro/events.js` exports an array of 11 entries
with shape:

```js
export const MACRO_EVENTS = [
  {
    id: 'feb26_normalization',
    date: '2026-02-01',          // first of event month
    category: 'NORMALIZATION',
    title: 'Reserves Rebuild · Macro Stability',
    summary: 'FX reserves cross $35bn. P2P inflation just above 9%.',
    color: '#2a8a59',            // dot color on DSEX
    kpiMetricIds: [              // for the 5 KPI rows
      'point_to_point_inflation_monthly',
      'bb_repo_rate_monthly',
      'gross_reserves_usd_bn_monthly',
      'usd_bdt_mid_monthly',
      'dsex_monthly',
    ],
  },
  // ... 10 more
];
```

Event cards are rendered from this list; KPI values are looked up at
`as_of = event.date` from `metric_history_monthly`. No narrative field —
graph-only per the design.

## Error Handling

| Failure | Behavior |
|---|---|
| Macro Observer unreachable during ingestion | Script exits non-zero with clear error. No partial writes (transaction-wrapped upsert batch). Re-run when source is back. |
| Macro Observer JSON schema changes | Validate expected top-level keys against `KEY_MAP`. Missing keys → warn + skip. New unknown keys → warn but proceed. |
| Supabase missing `metric_history_monthly` table | Script checks table existence on startup, prints `run migration 023_metric_history_monthly.sql first` and exits. |
| `/macro` page loads but Supabase returns empty | Page renders the section dividers and shows `no monthly history yet — run scripts/seed_macro_monthly.py` in place of charts. Same pattern as the existing `<div className="loading">no archive data yet…</div>`. |
| Chart.js CDN unreachable | `ensureChartJS()` rejects; page shows `Chart library failed to load. Check connection.` Existing nav still works. |
| One metric's data is missing or partial | The relevant `ChartCard` renders the title and a placeholder `data unavailable for this metric`. Other 12 charts unaffected. |
| Event modal mini-chart data window is empty | Modal still renders KPIs; mini-chart slot shows `insufficient surrounding data`. |
| Service worker caching Chart.js bundle that becomes stale | SW strategy is cache-first with network-update; a future Chart.js upgrade requires bumping the version-tag query param on the CDN URL. |

## Testing

| Layer | What | Tool |
|---|---|---|
| Ingestion script | `KEY_MAP` covers all expected source keys; missing-key handling; transformation correctness for sample series; upsert idempotency (re-run produces 0 changes) | pytest with mocked HTTP + mocked Supabase |
| Schema | Migration applies cleanly; constraints enforce `(metric_id, as_of)` unique; index exists | Supabase migration test |
| Frontend chart configs | Each config function returns a valid Chart.js config object on a sample monthly series; throws no errors on empty input | Vitest or jest if added; otherwise manual smoke |
| Frontend page render | `/macro` mounts; all 13 canvases instantiated; events list renders 11 entries; modal opens on click and renders 2 mini-canvases | Manual smoke + visual diff against Macro Observer |
| End-to-end | Visit `/macro` after seeding; charts render; click event → modal with charts; mobile viewport works | Manual on real device + dev browser |

**Out of scope for v1:** snapshot/visual-regression testing of charts,
automated cross-browser testing, accessibility automation. Worth adding
later but not blocking ship.

## Out of Scope / Future Work

- **Primary-source extraction** (BB Statistical Database, BBS bulletins,
  DSE archives) for replacing the Macro Observer seed metric-by-metric.
  Triggered by data-quality observations, not a scheduled migration.
- **Monthly aggregator from daily `metric_history`**: once EconDelta has
  6+ months of daily data per metric, add a job that computes month-end
  values and appends to `metric_history_monthly` so the system becomes
  self-sufficient and the seed becomes purely historical.
- **Unified daily-and-monthly time-series views** (single chart showing
  14-year monthly + last 90-day daily). Requires `UNION` queries across
  the two tables; defer until there's a concrete user need.
- **Macro Temperature gauge** + **driver cards** + **editorial NOTE
  blocks** — explicitly excluded by user; can be revisited later if the
  analytical layer becomes useful.
- **Event narratives** — same. Could be Opus-generated through the
  existing aggregate pipeline if the user later wants commentary.

## Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Macro Observer's JSON shape may differ from what was inferred from the rendered page; some `KEY_MAP` entries may be wrong on first run | Implementation step 1 is to fetch the JSON and inspect — finalise `KEY_MAP` against the actual payload before writing the bulk-upsert path. Spec acknowledges this. |
| Macro Observer site goes offline before we ingest | Cache the JSON locally as a one-time download checked into the repo (`scripts/_seed_data/macro_monthly_data.json`) so the seed is reproducible regardless of upstream availability. |
| Events list is hard-coded; new events require code change | Acceptable for v1 — events change infrequently (last update was Feb'26). If we accumulate >20 events, move to Supabase. |
| Chart.js 4.4.0 CDN URL becomes stale | Bump version in `ensureChartJS()` and SW cache; pin via SHA in a future hardening step if needed. |
| User loads `/macro` before script seed has run | Empty-state copy explains how to seed; not a hard error. |

## References

- Macro Observer: `https://macro.thenazmussakib.com/`
- Macro Observer data file: `https://macro.thenazmussakib.com/macro_monthly_data.json`
- Existing EconDelta archive page: `pwa/pages/archive.jsx`
- EconDelta Supabase writer: `utils/supabase_writer.py`
- EconDelta supabase client: `pwa/supabase-client.js`
- v3 catalog: `config/sources-v3.json`
- Chart.js 4.4.0 docs: `https://www.chartjs.org/docs/4.4.0/`

---

**End of design.** Approved by Adnan during brainstorming session 2026-05-05.
Next step: implementation plan via `superpowers:writing-plans`.
