# EconDelta PWA — Design Spec

**Date:** 2026-05-04
**Status:** Approved (brainstorming complete, awaiting implementation plan)
**Branch target:** `feat/v3-expansion` (or new branch off it)
**Repo:** `clauding-lab/econdelta`

---

## 1. Purpose & Scope

A static Progressive Web App that surfaces EconDelta's data in two modes:

- **Reference** — Adnan-as-banker checks current Bangladesh macro numbers (USD/BDT, reserves, DSEX, NPL ratio, food prices, etc.) on his iPhone, multiple times a day.
- **Operator** — Adnan-as-pipeline-owner monitors which scrapers ran, what failed, and how long things took.

Audience: Adnan + a small public-facing audience (banking colleagues who'll see the URL). Anonymous read-only access — no login.

**Scope: full 4-page bundle.** Latest, Archive, Runs, Sources/About. Public URL: `https://clauding-lab.github.io/econdelta/`.

**Out of scope:** authentication, user accounts, write operations from the browser, push notifications, payments, internationalisation.

---

## 2. Architecture

```
ExonVPS (adnan-local@103.187.23.22)
  systemd timers → scrapers + parse + aggregate
       │
       ▼
  aggregate_latest.py (extended)
    • upserts metric_history rows           (existing — done 2026-05-02 + 2026-05-04)
    • upserts metric_definitions rows       (NEW — once at startup, ON CONFLICT DO NOTHING)
    • inserts run_logs row per pipeline     (NEW — every fire)
  each scraper (extended)
    • inserts run_logs row at start + end   (NEW — wrap_run convention)
       │
       │ Supabase REST + RPC
       ▼
Supabase (ssbliukchgibjcjohibi) — same project as brief
   metric_history       ─┐
   metric_definitions    ├─→ get_latest_dashboard() RPC
   run_logs              ─┘
   RLS: anon read, service_role write
       │
       │ HTTPS (anon key, public)
       ▼
PWA at clauding-lab.github.io/econdelta/
  pwa/ subfolder in repo
  Deploy: GitHub Actions on push to main → Pages
```

Three layers, three deploys, three lifecycles:

1. **Backend** (ExonVPS Python) — `git pull` on the VPS deploys.
2. **Database** (Supabase) — apply migrations via Supabase MCP / CLI.
3. **Frontend** (static PWA) — push to `main` triggers GitHub Action.

**Critical contract:** the PWA only talks to Supabase via the anon key + the `get_latest_dashboard()` RPC + direct REST queries against the three RLS-anon-readable tables. No SSH, no service-role exposure to the browser.

---

## 3. Database Schema (3 new pieces)

### 3.1 Table — `metric_definitions`

Indicator catalog. One row per indicator. Edited in Supabase Studio for cosmetic tweaks; new rows added by `aggregate_latest.py` on first sight of a metric_id (with `ON CONFLICT DO NOTHING` so manual edits are preserved).

```sql
create table if not exists metric_definitions (
  metric_id      text primary key,
  label          text not null,
  short_label    text,
  unit           text,
  domain         text not null,
  sort_order     integer not null default 100,
  cadence        text,
  format         text default 'comma-2dp',
  description    text,
  source         text,
  source_url     text,
  is_hero        boolean default false,
  inverted       boolean default false,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

create index if not exists metric_definitions_domain_sort_idx
  on metric_definitions (domain, sort_order);

alter table metric_definitions enable row level security;
create policy "anon read definitions" on metric_definitions for select to anon using (true);
```

**Migration file:** `db/migrations/0002_metric_definitions.sql`

### 3.2 Table — `run_logs`

Per-scraper invocation audit. Powers the Runs page commit-graph.

```sql
create table if not exists run_logs (
  id           uuid primary key default gen_random_uuid(),
  source       text not null,
  started_at   timestamptz not null,
  finished_at  timestamptz,
  duration_ms  integer,
  status       text not null,
  exit_code    integer,
  error        text,
  attempt      integer not null default 1,
  host         text,
  unit         text,
  inserted_at  timestamptz not null default now()
);

create index if not exists run_logs_source_started_idx
  on run_logs (source, started_at desc);
create index if not exists run_logs_started_idx
  on run_logs (started_at desc);

alter table run_logs enable row level security;
create policy "anon read runs" on run_logs for select to anon using (true);
```

**Status values:** `ok` | `fail` | `stale` | `skip`
**Source values:** `bb_forex` | `dse_market` | `commodity_prices` | `fetch` | `parse` | `aggregate`

**Migration file:** `db/migrations/0003_run_logs.sql`

### 3.3 RPC — `get_latest_dashboard()`

Single round-trip the PWA's Latest page calls on open. Returns one jsonb blob with definitions, latest values per metric_id, and last-success per source.

```sql
create or replace function get_latest_dashboard()
returns jsonb language sql stable security invoker as $$
  select jsonb_build_object(
    'updated_at', now(),
    'definitions', (
      select jsonb_agg(to_jsonb(d) order by d.domain, d.sort_order)
      from metric_definitions d
    ),
    'values', (
      select jsonb_object_agg(
        metric_id,
        jsonb_build_object(
          'value', value,
          'as_of', as_of,
          'source_as_of', source_as_of
        )
      )
      from (
        select distinct on (metric_id) metric_id, value, as_of, source_as_of
        from metric_history
        order by metric_id, as_of desc
      ) latest
    ),
    'sources_status', (
      select jsonb_object_agg(
        source,
        jsonb_build_object(
          'status', status,
          'last_success', started_at,
          'duration_ms', duration_ms,
          'error', error
        )
      )
      from (
        select distinct on (source) source, status, started_at, duration_ms, error
        from run_logs
        order by source, started_at desc
      ) recent
    )
  );
$$;

grant execute on function get_latest_dashboard() to anon;
```

**Migration file:** `db/migrations/0004_get_latest_dashboard.sql`

**Archive page** uses direct REST against `metric_history` with date filters (no RPC).
**Runs page** uses direct REST against `run_logs` (90-day window, client-side reshape into commit-graph).

---

## 4. Backend Code Changes

### 4.1 Extension — `utils/supabase_writer.py`

Already has `upsert_metric_history()` (shipped 2026-05-04). Adding four helpers:

```python
def log_run_start(source: str, unit: str | None = None,
                  started_at: datetime | None = None) -> str:
    """Insert a starting row, return uuid for the matching log_run_end()."""

def log_run_end(run_id: str, started_at: datetime,
                status: str, exit_code: int = 0,
                error: str | None = None) -> None:
    """Update the row with finished_at, duration_ms, status, exit_code, error.
       Swallows network errors — a logging failure must not mask scrape outcome."""

def upsert_metric_definitions_seed(definitions: list[dict]) -> int:
    """INSERT ... ON CONFLICT (metric_id) DO NOTHING.
       First insert wins, manual Studio edits preserved.
       Returns count of NEW rows inserted."""

def wrap_run(source: str, unit: str, main_func: Callable[[], int]) -> int:
    """Wraps main(), maps exit code to status, swallows logging failures."""
```

`wrap_run` is the thin pattern — one line per scraper at `__main__`.

### 4.2 Extension — `aggregate_latest.py`

Already extended for `source_as_of` (shipped 2026-05-04). Two more changes:

(a) Auto-seed new metric definitions at startup. Walk `config/sources-v3.json` + the per-domain catalog already built by `scripts/build_catalog.py`. For each metric_id, build a default definition row. Call `upsert_metric_definitions_seed()`. Idempotent — only NEW metric_ids land; existing rows untouched.

```python
def _build_definition_seeds(catalog_entries: list[dict]) -> list[dict]:
    """Map indicator-catalog entries to metric_definitions rows.
       Defaults: sort_order=100, format='comma-2dp', is_hero=False.
       Tunable in Supabase Studio post-insert."""
```

(b) Use `wrap_run` at `__main__` so aggregate runs land in `run_logs`.

### 4.3 Instrumentation — 5 scrapers + 1 aggregator

One-line change per file:

```python
# before:
if __name__ == "__main__":
    sys.exit(main())

# after:
if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("bb_forex", "econdelta-forex.service", main))
```

| File | source tag | unit tag |
|---|---|---|
| `scrapers/bb_forex.py` | `bb_forex` | `econdelta-forex.service` |
| `scrapers/dse_market.py` | `dse_market` | `econdelta-dse.service` |
| `scrapers/commodity_prices.py` | `commodity_prices` | `econdelta-commodity.service` |
| `fetch_all.py` | `fetch` | `econdelta-fetch.service` |
| `parse_all.py` | `parse` | `econdelta-parse.service` |
| `aggregate_latest.py` | `aggregate` | `econdelta-aggregate.service` |

### 4.4 Status mapping

| Scraper exit code | run_logs.status | Meaning |
|---|---|---|
| 0 | `ok` | Wrote snapshot |
| 1 | `fail` | Exception raised |
| 2 | `stale` | Anomaly threshold tripped, write skipped (existing bb_forex.py convention) |
| 3 | `skip` | Non-trading day or no-op (existing dse_market.py convention) |
| other | `fail` | Unrecognized exit; `error` field populated |

### 4.5 Tests

| New test file | Coverage |
|---|---|
| `tests/test_run_logging.py` | log_run_start/end, wrap_run wrapping, swallowed network errors, status mapping |
| `tests/test_definitions_seed.py` | ON CONFLICT DO NOTHING idempotency, default field generation, new-metric flow |

Both fully mocked. No live Supabase needed (matches existing `test_supabase_writer.py`).

---

## 5. Frontend Structure (`pwa/` subfolder)

### 5.1 Files

```
econdelta/
└── pwa/
    ├── index.html                    (renamed from "EconDelta Dashboard.html")
    ├── config.js                     (Supabase URL + anon key — public, committed)
    ├── vendor/
    │   ├── react.production.min.js
    │   ├── react-dom.production.min.js
    │   └── babel.min.js
    ├── lib/
    │   ├── supabase-client.js        (rewritten — calls get_latest_dashboard RPC)
    │   └── data-mock.js              (renamed from data.js — kept for offline dev)
    ├── components.jsx                (shared UI — ported from bundle as-is)
    ├── pages/
    │   ├── latest.jsx                (rewritten — definitions-driven, hero+bento layout)
    │   ├── archive.jsx               (ported — direct REST against metric_history)
    │   ├── runs.jsx                  (ported — direct REST against run_logs)
    │   └── sources-about.jsx         (ported — definitions-driven sources list)
    ├── styles.css                    (ported as-is — IBM Plex triplet, terminal-newsprint)
    ├── sw.js                         (service worker — cache strategy below)
    ├── register-pwa.js               (ported as-is)
    ├── manifest.webmanifest          (see §6.2)
    └── icons/                        (ported from bundle/icons)
```

### 5.2 Stack — vanilla React UMD + Babel runtime

No build step. JSX parsed in the browser via `@babel/standalone`. React + Babel mirrored locally in `pwa/vendor/` (drops the unpkg.com dependency risk).

### 5.3 `index.html` script load order

```html
<script src="vendor/react.production.min.js"></script>
<script src="vendor/react-dom.production.min.js"></script>
<script src="vendor/babel.min.js"></script>

<script src="config.js"></script>
<script src="lib/supabase-client.js"></script>

<script type="text/babel" src="components.jsx"></script>
<script type="text/babel" src="pages/latest.jsx"></script>
<script type="text/babel" src="pages/archive.jsx"></script>
<script type="text/babel" src="pages/runs.jsx"></script>
<script type="text/babel" src="pages/sources-about.jsx"></script>
<script type="text/babel">/* App + createRoot */</script>

<script src="register-pwa.js" defer></script>
```

### 5.4 `config.js`

```javascript
window.ED_SUPABASE_CONFIG = {
  url: 'https://ssbliukchgibjcjohibi.supabase.co',
  anonKey: 'eyJ...',  // anon key — public by design, RLS-scoped to read-only
};
```

Anon key in browser is intentional and safe — Supabase anon keys are designed to be embedded in client code. RLS policies restrict it to `SELECT` on the three tables. Service-role keys stay on ExonVPS only.

### 5.5 Pages — what changes vs bundle

| Page | Bundle approach | New approach |
|---|---|---|
| Latest | 11 hardcoded tickers in 3 groups | Hero (4 most-watched) + bento grid (7 domain tiles), drill-in on tile tap, all driven by `definitions` array |
| Archive | 90-day window of forex/dse/commodity rows | 90-day window of `metric_history`, filterable by domain |
| Runs | Hardcoded 3-source commit-graph | Dynamic — render commit-graph for every distinct `source` in `run_logs` |
| Sources/About | Hardcoded 4-source description list | Iterate `metric_definitions` grouped by `source`, render per-source sections |

All four become **data-driven** — adding a new indicator means: scrape it, aggregator seeds the definition row, PWA picks it up next load. Zero PWA code change for new indicators.

### 5.6 Bundle files NOT used

| File | Why dropped |
|---|---|
| `econdelta_supabase.py` | Supersedes our `utils/supabase_writer.py` (current version integrated with hybrid parser, source_as_of, v3 architecture). Bundle's version assumes 5 source-typed Pydantic models that don't match. |
| `supabase-schema.sql` | Replaced by §3's three migrations. Bundle's 5-table schema doesn't match v3. |

---

## 6. UX / Design

### 6.1 Layout — Latest page (locked decision: D)

```
┌─────────────────────────────────────┐
│ EconDelta · Latest                  │
│ updated 2 min ago · 5 of 6 sources  │
├─────────────────────────────────────┤
│  ┌──────────┐  ┌──────────┐         │
│  │ USD/BDT  │  │  DSEX    │  HEROES │
│  │ 122.40   │  │  5,257   │         │
│  └──────────┘  └──────────┘         │
│  ┌──────────┐  ┌──────────┐         │
│  │ NPL %    │  │ Reserves │         │
│  │ 35.73    │  │ 28.03 bn │         │
│  └──────────┘  └──────────┘         │
├─────────────────────────────────────┤
│  ┌─────────┐ ┌─────────┐            │
│  │ Forex   │ │MoneyMkt │            │
│  │ 8 ind.  │ │ 7 ind.  │   BENTO    │
│  │ EUR ... │ │ 91d ... │   TILES    │
│  │ GBP ... │ │ 5y  ... │            │
│  └─────────┘ └─────────┘            │
│  ┌─────────┐ ┌─────────┐            │
│  │Inflatn  │ │DAM Food │            │
│  │ ...     │ │ ...     │            │
│  └─────────┘ └─────────┘            │
│  ┌─────────┐ ┌─────────┐            │
│  │Govt Fin │ │ Commod  │            │
│  │ ...     │ │ ...     │            │
│  └─────────┘ └─────────┘            │
└─────────────────────────────────────┘
```

Tap a bento tile → expanded view of that domain's full indicator list (route: `#/domain/forex`, `#/domain/banking`, etc. — needs an additional page or modal).

Hero card selection driven by `metric_definitions.is_hero = true` (default 4: USD/BDT mid, DSEX, banking_npl_pct, forex_reserves_gross_usd_bn).

### 6.2 Manifest

```json
{
  "name": "EconDelta — Bangladesh Macro",
  "short_name": "EconDelta",
  "description": "Live snapshot of Bangladesh forex, capital markets, banking stability, food prices, and government finance. Daily, anomaly-gated.",
  "start_url": "./",
  "scope": "./",
  "display": "standalone",
  "background_color": "#0e1418",
  "theme_color": "#c34a1f",
  "categories": ["finance", "productivity"],
  "icons": [
    { "src": "icons/icon-192.png",          "sizes": "192x192", "type": "image/png", "purpose": "any" },
    { "src": "icons/icon-512.png",          "sizes": "512x512", "type": "image/png", "purpose": "any" },
    { "src": "icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ]
}
```

### 6.3 Design system

Inherited from bundle as-is:
- Fonts: IBM Plex Mono / Sans / Serif (loaded from Google Fonts)
- Background: `#0e1418` (dark)
- Foreground: `#e8e2d2` (warm cream)
- Accent: `#c34a1f` (oxblood)
- OK: `#6abf6e`, alert: `#c34a1f`, neutral: `#6b7480`

---

## 7. Service Worker / Offline

Three caching tiers:

| Resource | Strategy | Why |
|---|---|---|
| `vendor/*` (React, Babel) | cache-first, never revalidate | Pinned, large, never change between deploys |
| `*.jsx, *.js, *.css, icons/*` | stale-while-revalidate | Show cached instantly, fetch new in background |
| RPC call `get_latest_dashboard()` | network-first, 5s timeout, cache fallback | Always try fresh; if offline, render cached + STALE banner |

Cache version baked into SW filename (`sw.js?v=YYYY-MM-DD-N`) — bumping forces SW update + cache eviction. Updated by deploy workflow on each push.

**Offline render:** PWA shows last-cached payload + banner: "showing cached data from N min ago — pull to retry."

**First install:** requires network (no SW cache yet). Native browser offline page if user is offline at first visit.

---

## 8. Deployment

### 8.1 Workflow — `.github/workflows/pwa-deploy.yml`

```yaml
name: Deploy PWA
on:
  push:
    branches: [main]
    paths: ['pwa/**', '.github/workflows/pwa-deploy.yml']
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: 'pages'
  cancel-in-progress: false

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with: { path: 'pwa' }
      - id: deployment
        uses: actions/deploy-pages@v4
```

### 8.2 One-time setup

1. GitHub repo → Settings → Pages → Source = "GitHub Actions"
2. Confirm anon key is in `pwa/config.js` (public by design)
3. First push to `main` triggers deploy → ~30s → live at `https://clauding-lab.github.io/econdelta/`

### 8.3 Update flow

| Type of change | Action |
|---|---|
| New indicator added in `sources-v3.json` | None — aggregator seeds definition next run, PWA picks up next load |
| Cosmetic tweak (label, sort order, hero promotion) | Edit row in Supabase Studio; live immediately |
| PWA code change | Commit to `main`, Action deploys in ~30s |
| Anon key rotation | Edit `pwa/config.js`, commit, push |

---

## 9. Error Handling

| Failure mode | UX |
|---|---|
| RPC returns 5xx / times out | SW cache fallback + STALE banner |
| RPC returns valid JSON but `definitions` empty | Empty state: "No indicators registered. Has the aggregator run?" |
| Single metric_id missing from `values` | Render its card with "—" + subtle "no data" label, not an error |
| `run_logs` empty (Runs page) | Empty state: "No pipeline runs recorded yet." |
| User offline at first install | Native browser offline page (unavoidable — first install requires network) |
| Anon key revoked / RLS broken | RPC returns 401/403 → banner: "Auth error. The API key may have been rotated." |

No try/catch swallowing. All RPC errors logged to `console.error`. No Sentry / no analytics in MVP.

---

## 10. Testing

| Layer | Approach | Tool |
|---|---|---|
| Backend Python (log_run, definitions seed, RPC interaction) | Mocked Supabase | pytest (existing pattern, see `tests/test_supabase_writer.py`) |
| SQL migrations | Apply to Supabase branch (free), `select * from get_latest_dashboard()` against seeded data, assert shape | Supabase MCP `apply_migration` + `execute_sql` |
| Frontend rendering | Manual visual checks against mock data layer | Open `pwa/index.html` locally with `?mock=1` |
| Service worker | Manual: install PWA on iPhone, kill network, verify cached render + STALE banner | Real device |
| End-to-end | Open production URL on iPhone, install to homescreen, verify install icon + standalone display + offline | Real device |

No Playwright e2e for MVP — overkill for a 4-page read-only dashboard. Add later if behavior gets complex.

---

## 11. Decisions Made (Locked)

| # | Decision | Rationale |
|---|---|---|
| 1 | Scope: full 4-page operator + reference dashboard, public-facing | C+D pick — covers both Adnan-as-banker and Adnan-as-operator + sharable with colleagues |
| 2 | Stack: vanilla React UMD + Babel runtime, no build step, vendor mirrored locally | Zero tooling, matches bundle, PWA caching mitigates first-paint cost; mirroring kills unpkg dependency risk |
| 3 | Data shape: full v3 catalog (60+ indicators across 7 domains) | Surfaces all the v3 expansion work since 2026-04 |
| 4 | Data layer: `metric_definitions` table + `get_latest_dashboard()` RPC | Banker-friendly Studio edits without redeploy; matches V6 brief RPC pattern; one round-trip; future apps reuse |
| 5 | Repo: `pwa/` subfolder in econdelta + GitHub Pages via Actions | One repo, one CLAUDE.md, frontend lifecycle inside the Python repo |
| 6 | Run logs: full `run_logs` Supabase table + instrument all 6 scrapers | Cleanest end state; single thin `wrap_run` helper makes per-scraper change one line |
| 7 | Layout: hero cards + bento grid | Highest information density above the fold; matches "phone, multiple times a day" usage pattern |
| 8 | Design system: inherit bundle's IBM Plex triplet + terminal-newsprint + oxblood accent | Already proven in mockups; no reason to redesign |
| 9 | Deploy: GitHub Actions on push to main, paths-filtered to `pwa/**` | No build step; ~30s deploy; doesn't trigger on Python backend changes |
| 10 | URL: `https://clauding-lab.github.io/econdelta/` | Free, HTTPS automatic, custom domain easy to add later |
| 11 | Testing: pytest backend + Supabase MCP for SQL + manual real-device PWA checks | Right-sized for a 4-page read-only dashboard |

---

## 12. Out of Scope (explicit non-goals)

- Authentication / user accounts (anon read-only is the model)
- Write operations from the browser
- Push notifications
- Payments / monetization
- Internationalization (English-only)
- E2E test automation (Playwright deferred)
- Custom domain (`clauding-lab.github.io/econdelta/` is fine for MVP)
- Per-scraper retry instrumentation (only entry-level wrap_run for now)
- Backfill of existing `metric_history` rows with `source_as_of` (separate one-shot migration, not part of this spec)
- Forex.service Akamai/BB blockade root-cause fix (separate investigation)
- Indicator re-classification or cleanup (existing 60+ catalog stays as-is)

---

## 13. Open Questions / Risks

- **Hero card selection:** initial defaults `is_hero = true` for USD/BDT mid, DSEX, banking_npl_pct, forex_reserves_gross_usd_bn. Adnan can edit `is_hero` in Supabase Studio later. If he wants different defaults, change in the seed code.
- **Domain tiles count:** 7 v3 domains → 7 tiles in a 2-col layout = 4 rows below the hero block. May need scroll. Acceptable.
- **Drill-in route shape:** `#/domain/<domain_slug>` → renders all metrics in that domain. New page file or component? Implementation plan decides.
- **Aggregate runs every 24h** → metric_definitions seed only checks for new metric_ids once per day. If you add an indicator at 11am BDT, PWA won't show it until 05:20 next morning. Fix: aggregate also seeds on `--once` invocation; or PWA shows "indicator not yet registered" placeholder. Defer to plan.
- **Service worker version bump:** the deploy workflow needs to update the `?v=` cache buster on every push. Either: a sed step in the Action, or a pre-commit hook, or accept one stale page load per user per deploy.
- **First install on cellular:** ~3.8MB first-load (React + Babel + app code). Mitigated by SW cache for repeat visits. Acceptable for personal + small-audience use.

---

## 14. Files Modified / Added Summary

### New files

| Path | Type |
|---|---|
| `pwa/index.html` | Frontend |
| `pwa/config.js` | Frontend (public anon key) |
| `pwa/vendor/react.production.min.js` | Vendor mirror |
| `pwa/vendor/react-dom.production.min.js` | Vendor mirror |
| `pwa/vendor/babel.min.js` | Vendor mirror |
| `pwa/lib/supabase-client.js` | Frontend (RPC client) |
| `pwa/lib/data-mock.js` | Frontend (dev only) |
| `pwa/components.jsx` | Frontend |
| `pwa/pages/latest.jsx` | Frontend |
| `pwa/pages/archive.jsx` | Frontend |
| `pwa/pages/runs.jsx` | Frontend |
| `pwa/pages/sources-about.jsx` | Frontend |
| `pwa/styles.css` | Frontend |
| `pwa/sw.js` | Frontend |
| `pwa/register-pwa.js` | Frontend |
| `pwa/manifest.webmanifest` | Frontend |
| `pwa/icons/*` (6 PNGs) | Frontend |
| `db/migrations/0002_metric_definitions.sql` | DDL |
| `db/migrations/0003_run_logs.sql` | DDL |
| `db/migrations/0004_get_latest_dashboard.sql` | DDL |
| `tests/test_run_logging.py` | Backend test |
| `tests/test_definitions_seed.py` | Backend test |
| `.github/workflows/pwa-deploy.yml` | CI |
| `docs/superpowers/specs/2026-05-04-econdelta-pwa-design.md` | This document |

### Modified files

| Path | Change |
|---|---|
| `utils/supabase_writer.py` | Add 4 helpers: log_run_start, log_run_end, upsert_metric_definitions_seed, wrap_run |
| `aggregate_latest.py` | Add definitions seed call; use wrap_run in __main__ |
| `parse_all.py` | Use wrap_run in __main__ |
| `fetch_all.py` | Use wrap_run in __main__ |
| `scrapers/bb_forex.py` | Use wrap_run in __main__ |
| `scrapers/dse_market.py` | Use wrap_run in __main__ |
| `scrapers/commodity_prices.py` | Use wrap_run in __main__ |

**Total: ~24 new files, ~7 modified files, ~250 lines new code, ~30 lines modified, ~75 lines new SQL.**

---

## 15. Estimated Effort

Rough sizing for the implementation plan:

| Phase | Estimate |
|---|---|
| 1. SQL migrations + RPC + RLS verification | 1.5 hours |
| 2. Backend helpers + wrap_run + tests | 2 hours |
| 3. Aggregator extension (definitions seed) + tests | 1 hour |
| 4. Scraper instrumentation (6 files, ~5 lines each) | 30 min |
| 5. Vendor mirroring + index.html + config.js | 30 min |
| 6. supabase-client.js rewrite for RPC | 1.5 hours |
| 7. Latest page rewrite (hero + bento + drill-in) | 3 hours |
| 8. Archive + Runs + Sources/About page adaptation | 2 hours |
| 9. Service worker tuning + cache version bump | 1 hour |
| 10. GitHub Actions workflow + first deploy | 30 min |
| 11. Manual real-device testing on iPhone | 1 hour |
| 12. Polish + bug fixes | 2 hours |
| **Total** | **~16 hours** |

Spread across 3-4 focused sessions of ~4-5 hours each.
