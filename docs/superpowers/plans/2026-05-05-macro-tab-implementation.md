---
date: 2026-05-05
project: EconDelta
spec: docs/superpowers/specs/2026-05-05-macro-tab-long-horizon-design.md
status: ready-to-execute
---

# `/macro` Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a new `/macro` tab in the EconDelta PWA that renders 13 long-horizon monthly charts (Jan 2012 → latest) plus 11 click-to-open event modals, fed by a new `metric_history_monthly` Supabase table that is one-shot seeded from Macro Observer's public `macro_monthly_data.json`.

**Architecture:** One-shot Python ingestion script writes to two new Supabase tables (`metric_history_monthly`, `metric_definitions_monthly`) — fully isolated from the existing daily `metric_history`. The PWA gets a new page (`pwa/pages/macro.jsx`) that lazy-loads Chart.js 4.4.0 from CDN on first visit, fetches monthly history via PostgREST, and renders 13 chart cards + 11 events. Existing pages, scrapers, and parsers are untouched.

**Tech Stack:** Python 3.12 + `requests` for ingestion · Supabase Postgres (project `ssbliukchgibjcjohibi`) · React via `text/babel` browser-mode (no build step) · Chart.js 4.4.0 (UMD, CDN) · pytest for backend tests · manual visual smoke for frontend.

---

## Pre-Flight Conventions

Two corrections to spec wording that the implementer must follow (the spec was written before these were verified against the codebase):

1. **Migration directory** is `db/migrations/`, NOT `supabase/migrations/`. Numbering is 4-digit zero-padded. Latest existing is `0005_metric_history_anon_read.sql`. New migrations are `0006_metric_history_monthly.sql` and `0007_metric_definitions_monthly.sql` (the spec's `023/024` numbers are wrong for this project).
2. **PWA uses inline-Babel + globals**, not ES modules. `pwa/components.jsx` ends with `Object.assign(window, { ... })` exposing globals. `chartConfigs.js` and `events.js` MUST expose a `window.MACRO_*` namespace, not `export`. Chart.js loads via `<script>` tag injection at runtime, not `import()`.

## Working Branch

A single branch `feat/macro-tab` rooted off the existing `spec/macro-tab-long-horizon` branch (which holds the design spec at commit `c9d368c`). All work commits onto `feat/macro-tab`. PR opens against `main` at the end.

```bash
git checkout spec/macro-tab-long-horizon
git checkout -b feat/macro-tab
```

## Approval Gates (per Adnan's standing rule)

Shared-state actions that require **per-action explicit approval before invocation**:

- `mcp__plugin_supabase_supabase__apply_migration` (Phase 1, Tasks 2 & 3)
- `python -m scripts.seed_macro_monthly` against live Supabase (Phase 2, Task 8)
- `gh pr merge` for the final PR (Phase 7, Task 17)
- Any `ssh adnan-local@103.187.23.22 ...` commands (none planned in this plan, but if seed needs to run on VPS instead of laptop)

The implementing subagent must STOP and ask the user before each, with action-explicit language.

## File Structure

### Created
| File | Responsibility |
|---|---|
| `db/migrations/0006_metric_history_monthly.sql` | Schema for the long-horizon monthly history table + anon-read RLS |
| `db/migrations/0007_metric_definitions_monthly.sql` | Schema for the monthly metric catalog + anon-read RLS |
| `scripts/seed_macro_monthly.py` | One-shot ingestion: fetch JSON → transform via `KEY_MAP` → upsert |
| `scripts/_seed_data/macro_monthly_data.json` | Local cached copy of upstream JSON (committed for reproducibility) |
| `tests/test_seed_macro_monthly.py` | Pytest suite with mocked HTTP + mocked Supabase |
| `pwa/pages/macro.jsx` | Page entry; mounts ChartCards, EventStrip, EventModal |
| `pwa/pages/macro/chartConfigs.js` | 13 chart-config builder functions + 2 mini-chart functions; exposes `window.MACRO_CHART_CONFIGS` |
| `pwa/pages/macro/events.js` | 11 event entries; exposes `window.MACRO_EVENTS` |

### Modified
| File | Change |
|---|---|
| `pwa/index.html` | Add `<script>` tags for chartConfigs, events, macro.jsx; add `/macro` route |
| `pwa/components.jsx` | Add Macro nav entry to the `items` array |
| `pwa/sw.js` | Bump `CACHE_NAME`; add new files to `APP_SHELL` |

### Untouched (sanity-check guarantees)
- `metric_history`, `metric_definitions` tables and their migrations 0001–0005
- All scrapers in `scrapers/`, parsers in `parsers/`, `aggregate_latest.py`, `fetch_all.py`, `parse_all.py`
- `pwa/pages/{latest,archive,runs,sources-about}.jsx`
- `pwa/lib/supabase-client.js` (the macro page does its own PostgREST fetch — see Decision Note below)
- `utils/supabase_writer.py` (the seed script writes via its own helper; reusing the writer would require a `table_name` parameter that doesn't exist and isn't worth adding for one caller)

### Decision Note: why macro.jsx fetches monthly data itself

The existing `pwa/lib/supabase-client.js` `bootstrap()` function pulls 90-day daily history for *every* page load. We do NOT want to add `metric_history_monthly` (~4,800 rows) to every page load — only `/macro` needs it. So `macro.jsx` does its own PostgREST GET against `metric_history_monthly` on mount, stashes the result in `window.ED_DATA.macroMonthly`, and re-renders. This is the same `cfg = window.ED_SUPABASE_CONFIG` pattern the existing client uses.

---

## Phase 0 — KEY_MAP Discovery

Goal: confirm the actual JSON shape before writing migrations or code. The spec flags this as risk #1.

### Task 1: Fetch and inspect `macro_monthly_data.json`

**Files:**
- Create: `scripts/_seed_data/macro_monthly_data.json`
- Create: `scripts/_seed_data/SHAPE_NOTES.md` (one-pager describing the actual JSON keys vs the spec's `KEY_MAP` design intent)

- [ ] **Step 1: Download the JSON to the seed-data cache**

```bash
mkdir -p scripts/_seed_data
curl -sSL "https://macro.thenazmussakib.com/macro_monthly_data.json" \
  -o scripts/_seed_data/macro_monthly_data.json
ls -la scripts/_seed_data/macro_monthly_data.json
wc -c scripts/_seed_data/macro_monthly_data.json
```

Expected: file 50–500 KB. If `curl` returns HTML (server error or login wall), STOP and report — Macro Observer may have changed access policy.

- [ ] **Step 2: Inspect top-level structure**

```bash
python3 -c "
import json
with open('scripts/_seed_data/macro_monthly_data.json') as f:
    d = json.load(f)
print('Top-level type:', type(d).__name__)
if isinstance(d, dict):
    for k, v in d.items():
        sample = ''
        if isinstance(v, list) and v:
            sample = f' first={v[0]!r}'
        elif isinstance(v, dict):
            sample = f' keys={list(v.keys())[:5]}'
        print(f'  {k}: {type(v).__name__} (len={len(v) if hasattr(v, \"__len__\") else \"?\"}){sample}')
"
```

Expected output: a list of top-level keys with their types/sizes. Compare each key against the `KEY_MAP` in the spec (lines 402–456).

- [ ] **Step 3: Document the actual shape**

Write `scripts/_seed_data/SHAPE_NOTES.md` with:
- The full list of actual top-level keys observed
- For each key: type (list / dict / scalar), length, sample first datapoint
- A `KEY_MAP_ADJUSTMENTS` table: spec-name → actual-name (or `MISSING` / `UNEXPECTED`) → resolution
- A clear note on the date format observed (e.g. `"2024-03-01"` vs `"2024-03"` vs `[2024, 3]`) — this drives the `as_of` parsing in Task 5

Example template:

```markdown
# Macro Observer JSON shape — captured 2026-05-05

Top-level: dict, N keys
File size: XXX KB

## Observed keys

| key | type | len | first datapoint |
|---|---|---|---|
| cpi_p2p_general | list of [date, value] | 170 | ["2012-01-01", 10.51] |
| ... | ... | ... | ... |

## Date format

`YYYY-MM-DD` (always day-1 of month). [or whatever it is]

## KEY_MAP adjustments vs spec

| spec key | actual key | resolution |
|---|---|---|
| cpi_p2p_general | cpi_p2p_general | OK |
| yield_curve | yields_term_structure | RENAME → `yields_term_structure` |
| (no spec entry) | call_money_rate_monthly | UNEXPECTED — skip for v1 |
| dsex | dsex_index | RENAME → `dsex_index` |
```

- [ ] **Step 4: Commit**

```bash
git add scripts/_seed_data/macro_monthly_data.json scripts/_seed_data/SHAPE_NOTES.md
git commit -m "chore(macro): cache macro_monthly_data.json and document shape"
```

---

## Phase 1 — Schema Migrations

### Task 2: Migration 0006 — `metric_history_monthly`

**Files:**
- Create: `db/migrations/0006_metric_history_monthly.sql`

- [ ] **Step 1: Write migration**

```sql
-- ============================================================================
-- 0006 — metric_history_monthly
-- ----------------------------------------------------------------------------
-- Long-horizon monthly observations for the /macro tab. Mirrors metric_history
-- shape but with monthly granularity. Seeded by scripts/seed_macro_monthly.py
-- from Macro Observer's public macro_monthly_data.json.
--
-- Stays separate from metric_history so existing daily queries don't have to
-- learn about a granularity column. Future monthly-aggregator-from-daily
-- writes target this table; no migration risk to the operational pipeline.
-- ============================================================================

create table if not exists metric_history_monthly (
  id              bigserial primary key,
  metric_id       text not null,
  as_of           date not null,           -- always day 1 of month, e.g. 2024-03-01
  value           numeric not null,
  source          text not null,           -- 'macro_observer_seed' for v1
  source_as_of    date,                    -- when the source published this datapoint
  ingested_at     timestamptz not null default now(),
  notes           text,
  unique (metric_id, as_of)
);

create index if not exists idx_mhm_metric_asof
  on metric_history_monthly (metric_id, as_of desc);

alter table metric_history_monthly enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where policyname = 'anon read metric_history_monthly'
  ) then
    create policy "anon read metric_history_monthly"
      on metric_history_monthly for select to anon using (true);
  end if;
end $$;

comment on table metric_history_monthly is
  'Long-horizon monthly observations for the /macro tab. Seeded from Macro Observer JSON.';
comment on column metric_history_monthly.as_of is
  'Always normalised to day-1 of the month (e.g. 2024-03-01).';
comment on column metric_history_monthly.source_as_of is
  'When the upstream source originally published this datapoint, where known.';
```

- [ ] **Step 2: Apply migration to Supabase (REQUIRES USER APPROVAL)**

STOP and ask the user: "Apply migration `0006_metric_history_monthly` to Supabase project `ssbliukchgibjcjohibi`? It creates one new table, one index, and one RLS policy. Reversible via `drop table metric_history_monthly cascade;`."

After approval:

```
mcp__plugin_supabase_supabase__apply_migration
  project_id: ssbliukchgibjcjohibi
  name: 0006_metric_history_monthly
  query: <contents of db/migrations/0006_metric_history_monthly.sql>
```

Expected: `{"success": true}`.

- [ ] **Step 3: Verify table exists**

```
mcp__plugin_supabase_supabase__execute_sql
  project_id: ssbliukchgibjcjohibi
  query: select table_name, column_name, data_type, is_nullable
         from information_schema.columns
         where table_name = 'metric_history_monthly'
         order by ordinal_position;
```

Expected: 8 columns (`id`, `metric_id`, `as_of`, `value`, `source`, `source_as_of`, `ingested_at`, `notes`).

- [ ] **Step 4: Commit**

```bash
git add db/migrations/0006_metric_history_monthly.sql
git commit -m "feat(db): add metric_history_monthly migration for /macro tab"
```

### Task 3: Migration 0007 — `metric_definitions_monthly`

**Files:**
- Create: `db/migrations/0007_metric_definitions_monthly.sql`

- [ ] **Step 1: Write migration**

```sql
-- ============================================================================
-- 0007 — metric_definitions_monthly
-- ----------------------------------------------------------------------------
-- Catalog for the monthly long-horizon metrics used by the /macro tab.
-- Mirrors metric_definitions but kept separate so daily-pipeline rows and
-- monthly-historical rows don't share a flat namespace.
--
-- Seeded by scripts/seed_macro_monthly.py alongside metric_history_monthly.
-- ============================================================================

create table if not exists metric_definitions_monthly (
  metric_id           text primary key,
  display_name        text not null,
  unit                text not null,         -- '%', 'BDT bn', 'USD bn', 'index', 'mo', 'BDT', 'USD mn', 'BDT mn'
  source_url          text,
  source_attribution  text,                  -- 'Nazmus Sakib · BB · BBS · DSE'
  domain              text not null,         -- 'prices_policy' | 'credit_money' | 'external' | 'capital_market'
  description         text,
  notes               text,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create index if not exists idx_mdm_domain on metric_definitions_monthly (domain);

alter table metric_definitions_monthly enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where policyname = 'anon read metric_definitions_monthly'
  ) then
    create policy "anon read metric_definitions_monthly"
      on metric_definitions_monthly for select to anon using (true);
  end if;
end $$;

comment on table metric_definitions_monthly is
  'Catalog of long-horizon monthly metrics surfaced on the /macro tab.';
comment on column metric_definitions_monthly.domain is
  'One of: prices_policy, credit_money, external, capital_market. Validated in app code.';
```

- [ ] **Step 2: Apply migration to Supabase (REQUIRES USER APPROVAL)**

STOP and ask: "Apply migration `0007_metric_definitions_monthly` to Supabase project `ssbliukchgibjcjohibi`? Same shape as 0006 — one table, one index, one RLS policy. Reversible via `drop table metric_definitions_monthly cascade;`."

After approval:

```
mcp__plugin_supabase_supabase__apply_migration
  project_id: ssbliukchgibjcjohibi
  name: 0007_metric_definitions_monthly
  query: <contents of db/migrations/0007_metric_definitions_monthly.sql>
```

Expected: `{"success": true}`.

- [ ] **Step 3: Verify**

```
mcp__plugin_supabase_supabase__execute_sql
  project_id: ssbliukchgibjcjohibi
  query: select count(*) from metric_definitions_monthly;
```

Expected: `0` (table exists, empty).

- [ ] **Step 4: Commit**

```bash
git add db/migrations/0007_metric_definitions_monthly.sql
git commit -m "feat(db): add metric_definitions_monthly migration for /macro tab"
```

---

## Phase 2 — Ingestion Script (TDD)

### Task 4: Write failing tests for `KEY_MAP` and row builder

**Files:**
- Create: `tests/test_seed_macro_monthly.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for scripts.seed_macro_monthly.

Mocks requests + supabase upsert. Verifies:
  - KEY_MAP entries are well-formed (frozen dataclass, valid domain)
  - Date parsing normalises to day-1 of month
  - Multi-tenor explosion (e.g. yield_curve → 5 metric_ids)
  - Skipped/unknown keys are warned but don't crash
  - Bulk upsert receives the expected payload shape
  - Idempotency: re-running with the same input produces the same rows
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from scripts.seed_macro_monthly import (
    DOMAIN_VALUES,
    KEY_MAP,
    MetricMap,
    build_history_rows,
    build_definitions_rows,
    normalise_as_of,
)


class TestKeyMap:
    def test_all_entries_are_metricmap_or_dict(self):
        for key, val in KEY_MAP.items():
            assert isinstance(val, (MetricMap, dict)), \
                f"{key} is {type(val).__name__}, expected MetricMap or dict"

    def test_all_domains_are_valid(self):
        for key, val in KEY_MAP.items():
            entries = val.values() if isinstance(val, dict) else [val]
            for m in entries:
                assert m.domain in DOMAIN_VALUES, \
                    f"{key} domain {m.domain!r} not in {DOMAIN_VALUES}"

    def test_all_metric_ids_unique(self):
        seen = set()
        for key, val in KEY_MAP.items():
            entries = val.values() if isinstance(val, dict) else [val]
            for m in entries:
                assert m.metric_id not in seen, \
                    f"duplicate metric_id: {m.metric_id}"
                seen.add(m.metric_id)


class TestNormaliseAsOf:
    def test_day1_passthrough(self):
        assert normalise_as_of("2024-03-01") == date(2024, 3, 1)

    def test_mid_month_clamps_to_day1(self):
        assert normalise_as_of("2024-03-15") == date(2024, 3, 1)

    def test_end_of_month_clamps_to_day1(self):
        assert normalise_as_of("2024-03-31") == date(2024, 3, 1)

    def test_year_month_only(self):
        assert normalise_as_of("2024-03") == date(2024, 3, 1)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            normalise_as_of("not-a-date")


class TestBuildHistoryRows:
    def test_simple_series(self):
        # Pick the first scalar entry from KEY_MAP as the test target.
        scalar_key = next(
            k for k, v in KEY_MAP.items() if isinstance(v, MetricMap)
        )
        scalar_metric = KEY_MAP[scalar_key].metric_id
        payload = {scalar_key: [["2012-01-01", 10.51], ["2012-02-01", 10.62]]}

        rows = build_history_rows(payload, source="macro_observer_seed")

        assert len(rows) == 2
        assert rows[0] == {
            "metric_id": scalar_metric,
            "as_of": "2012-01-01",
            "value": 10.51,
            "source": "macro_observer_seed",
            "source_as_of": "2012-01-01",
        }

    def test_multi_tenor_explosion(self):
        # Find a multi-tenor entry (yield_curve in spec, but use whatever's in KEY_MAP)
        multi_key = next(
            (k for k, v in KEY_MAP.items() if isinstance(v, dict)), None
        )
        if multi_key is None:
            pytest.skip("KEY_MAP has no multi-tenor entries")
        tenor_map = KEY_MAP[multi_key]
        # Build a payload shaped as the seed script expects multi-tenor input.
        # NOTE: shape will be confirmed in Task 1; for now assume:
        #   {multi_key: {"<tenor>": [[date, value], ...]}}
        first_tenor = next(iter(tenor_map.keys()))
        payload = {multi_key: {first_tenor: [["2012-01-01", 8.5]]}}

        rows = build_history_rows(payload, source="macro_observer_seed")

        assert len(rows) == 1
        assert rows[0]["metric_id"] == tenor_map[first_tenor].metric_id
        assert rows[0]["value"] == 8.5

    def test_unknown_key_is_skipped_with_warning(self, caplog):
        payload = {"definitely_not_in_keymap_xyz": [["2024-01-01", 1.0]]}

        rows = build_history_rows(payload, source="macro_observer_seed")

        assert rows == []
        assert any("definitely_not_in_keymap_xyz" in r.message for r in caplog.records)

    def test_skips_non_numeric_values(self):
        scalar_key = next(
            k for k, v in KEY_MAP.items() if isinstance(v, MetricMap)
        )
        payload = {scalar_key: [["2012-01-01", None], ["2012-02-01", 10.5]]}

        rows = build_history_rows(payload, source="macro_observer_seed")

        assert len(rows) == 1
        assert rows[0]["value"] == 10.5


class TestBuildDefinitionsRows:
    def test_one_row_per_metric_id(self):
        rows = build_definitions_rows()
        # Total metric_ids across KEY_MAP (counting multi-tenor explosions)
        expected = sum(
            len(v) if isinstance(v, dict) else 1
            for v in KEY_MAP.values()
        )
        assert len(rows) == expected

    def test_required_fields_present(self):
        rows = build_definitions_rows()
        for r in rows:
            assert r["metric_id"]
            assert r["display_name"]
            assert r["unit"]
            assert r["domain"] in DOMAIN_VALUES
            assert r["source_attribution"]
```

- [ ] **Step 2: Run tests — verify they fail with import error**

```bash
pytest tests/test_seed_macro_monthly.py -v --no-cov 2>&1 | head -30
```

Expected: `ImportError: cannot import name 'KEY_MAP' from 'scripts.seed_macro_monthly'` (module doesn't exist yet). Or `ModuleNotFoundError`.

- [ ] **Step 3: Commit (RED)**

```bash
git add tests/test_seed_macro_monthly.py
git commit -m "test(macro): failing tests for seed_macro_monthly KEY_MAP and row builders"
```

### Task 5: Implement `KEY_MAP`, `MetricMap`, `normalise_as_of`, row builders

**Files:**
- Create: `scripts/seed_macro_monthly.py`

- [ ] **Step 1: Write the module skeleton + KEY_MAP + helpers (NO main yet)**

```python
"""Seed Supabase metric_history_monthly + metric_definitions_monthly from
Macro Observer's public macro_monthly_data.json.

Run modes:
    python -m scripts.seed_macro_monthly --dry-run    # show what would change
    python -m scripts.seed_macro_monthly              # execute
    python -m scripts.seed_macro_monthly --refresh    # force re-fetch upstream

Idempotent: re-running with the same input produces zero changes downstream.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import requests

logger = logging.getLogger("seed_macro_monthly")

# Adjust this constant after Task 1 confirms the actual JSON shape.
UPSTREAM_URL = "https://macro.thenazmussakib.com/macro_monthly_data.json"
LOCAL_CACHE = Path(__file__).resolve().parent / "_seed_data" / "macro_monthly_data.json"
DEFAULT_SOURCE = "macro_observer_seed"
SOURCE_ATTRIBUTION = "Nazmus Sakib · BB · BBS · DSE"
SOURCE_URL = "https://macro.thenazmussakib.com/"

DOMAIN_VALUES = frozenset({"prices_policy", "credit_money", "external", "capital_market"})


@dataclass(frozen=True)
class MetricMap:
    metric_id: str
    display_name: str
    unit: str
    domain: str
    notes: str = ""

    def __post_init__(self) -> None:
        if self.domain not in DOMAIN_VALUES:
            raise ValueError(f"domain {self.domain!r} not in {DOMAIN_VALUES}")


# KEY_MAP: design-intent from spec. Confirmed/adjusted against actual payload
# in scripts/_seed_data/SHAPE_NOTES.md (Task 1). Update entries below to match
# the actual upstream key names, removing any that don't appear and adding any
# that do but weren't in the spec design.
KEY_MAP: dict[str, MetricMap | dict[str, MetricMap]] = {
    "cpi_p2p_general":          MetricMap("point_to_point_inflation_monthly",
                                          "CPI YoY (general)", "%", "prices_policy"),
    "cpi_p2p_food":             MetricMap("cpi_p2p_food_monthly",
                                          "CPI YoY (food)", "%", "prices_policy"),
    "cpi_p2p_nonfood":          MetricMap("cpi_p2p_nonfood_monthly",
                                          "CPI YoY (non-food)", "%", "prices_policy"),
    "cpi_12m_general":          MetricMap("cpi_12m_avg_monthly",
                                          "CPI 12-month average", "%", "prices_policy"),
    "repo_rate":                MetricMap("bb_repo_rate_monthly",
                                          "BB repo rate", "%", "prices_policy"),
    "tbill_364d":               MetricMap("tbill_364d_yield_monthly",
                                          "364-day T-bill yield", "%", "prices_policy"),
    "yield_curve": {
        "1y":  MetricMap("yield_1y_monthly",  "1Y yield",  "%", "prices_policy"),
        "2y":  MetricMap("yield_2y_monthly",  "2Y yield",  "%", "prices_policy"),
        "5y":  MetricMap("yield_5y_monthly",  "5Y yield",  "%", "prices_policy"),
        "10y": MetricMap("yield_10y_monthly", "10Y yield", "%", "prices_policy"),
        "20y": MetricMap("yield_20y_monthly", "20Y yield", "%", "prices_policy"),
    },
    "real_policy_rate":         MetricMap("real_policy_rate_monthly",
                                          "Real policy rate", "%", "prices_policy"),
    "domestic_credit_total":    MetricMap("domestic_credit_total_monthly",
                                          "Total domestic credit", "BDT bn", "credit_money"),
    "domestic_credit_public":   MetricMap("domestic_credit_public_monthly",
                                          "Public-sector domestic credit", "BDT bn", "credit_money"),
    "domestic_credit_private":  MetricMap("domestic_credit_private_monthly",
                                          "Private-sector domestic credit", "BDT bn", "credit_money"),
    "private_credit_growth_yoy": MetricMap("private_credit_growth_yoy_monthly",
                                          "Private credit growth YoY", "%", "credit_money"),
    "public_credit_growth_yoy": MetricMap("public_credit_growth_yoy_monthly",
                                          "Public credit growth YoY", "%", "credit_money"),
    "m1_growth_yoy":            MetricMap("m1_growth_yoy_monthly",
                                          "M1 growth YoY", "%", "credit_money"),
    "m2_growth_yoy":            MetricMap("m2_growth_yoy_monthly",
                                          "M2 growth YoY", "%", "credit_money"),
    "exports_usd_mn":           MetricMap("exports_usd_mn_monthly",
                                          "Exports", "USD mn", "external"),
    "imports_usd_mn":           MetricMap("imports_usd_mn_monthly",
                                          "Imports", "USD mn", "external"),
    "remittance_usd_mn":        MetricMap("remittance_usd_mn_monthly",
                                          "Remittance", "USD mn", "external"),
    "fx_reserves_gross_bn":     MetricMap("gross_reserves_usd_bn_monthly",
                                          "FX reserves (gross)", "USD bn", "external"),
    "import_cover_months":      MetricMap("import_cover_months_monthly",
                                          "Import cover", "mo", "external"),
    "bdt_usd":                  MetricMap("usd_bdt_mid_monthly",
                                          "BDT / USD", "BDT", "external"),
    "reer":                     MetricMap("reer_monthly",
                                          "REER (100 baseline)", "index", "external"),
    "dsex":                     MetricMap("dsex_monthly",
                                          "DSEX index", "index", "capital_market"),
    "dsex_turnover":            MetricMap("dsex_turnover_monthly",
                                          "DSEX daily turnover", "BDT mn", "capital_market"),
}


def normalise_as_of(raw: str) -> date:
    """Coerce any source date into the first day of its month.

    Accepts: 'YYYY-MM-DD', 'YYYY-MM'. Rejects anything else.
    """
    parts = raw.split("-")
    if len(parts) == 2:
        y, m = parts
        return date(int(y), int(m), 1)
    if len(parts) == 3:
        y, m, _d = parts
        return date(int(y), int(m), 1)
    raise ValueError(f"cannot parse date {raw!r}")


def _iter_pairs(series: Any) -> Iterable[tuple[str, float]]:
    """Yield (date_str, value) from a series. Handles list-of-pairs and dict-of-date->value."""
    if isinstance(series, list):
        for pair in series:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                continue
            yield pair[0], pair[1]
    elif isinstance(series, dict):
        for d, v in series.items():
            yield d, v


def _row_for(metric: MetricMap, raw_date: str, raw_value: Any, source: str) -> dict | None:
    if raw_value is None or isinstance(raw_value, bool):
        return None
    if not isinstance(raw_value, (int, float)):
        return None
    try:
        as_of = normalise_as_of(raw_date)
    except ValueError:
        logger.warning("skipping unparseable date %r for metric %s", raw_date, metric.metric_id)
        return None
    return {
        "metric_id": metric.metric_id,
        "as_of": as_of.isoformat(),
        "value": float(raw_value),
        "source": source,
        "source_as_of": as_of.isoformat(),
    }


def build_history_rows(payload: dict, *, source: str = DEFAULT_SOURCE) -> list[dict]:
    """Transform raw upstream JSON into PostgREST upsert rows for metric_history_monthly."""
    rows: list[dict] = []
    for upstream_key, value in payload.items():
        target = KEY_MAP.get(upstream_key)
        if target is None:
            logger.warning("upstream key %r has no KEY_MAP entry; skipping", upstream_key)
            continue
        if isinstance(target, MetricMap):
            for raw_date, raw_value in _iter_pairs(value):
                row = _row_for(target, raw_date, raw_value, source)
                if row is not None:
                    rows.append(row)
        elif isinstance(target, dict):
            # Multi-tenor: value is dict of tenor -> series
            if not isinstance(value, dict):
                logger.warning("multi-tenor key %r: expected dict, got %s; skipping",
                               upstream_key, type(value).__name__)
                continue
            for tenor, series in value.items():
                metric = target.get(tenor)
                if metric is None:
                    logger.warning("unknown tenor %r under %r; skipping", tenor, upstream_key)
                    continue
                for raw_date, raw_value in _iter_pairs(series):
                    row = _row_for(metric, raw_date, raw_value, source)
                    if row is not None:
                        rows.append(row)
    return rows


def build_definitions_rows() -> list[dict]:
    """One row per known metric_id in KEY_MAP, including multi-tenor explosions."""
    rows: list[dict] = []
    for upstream_key, target in KEY_MAP.items():
        entries = target.values() if isinstance(target, dict) else [target]
        for m in entries:
            rows.append({
                "metric_id":          m.metric_id,
                "display_name":       m.display_name,
                "unit":               m.unit,
                "source_url":         SOURCE_URL,
                "source_attribution": SOURCE_ATTRIBUTION,
                "domain":             m.domain,
                "description":        f"Long-horizon monthly series. Upstream key: {upstream_key}.",
                "notes":              m.notes or None,
            })
    return rows
```

- [ ] **Step 2: Run tests — verify they pass**

```bash
pytest tests/test_seed_macro_monthly.py -v --no-cov 2>&1 | tail -30
```

Expected: all `TestKeyMap`, `TestNormaliseAsOf`, `TestBuildHistoryRows`, `TestBuildDefinitionsRows` tests PASS. If `TestBuildHistoryRows::test_multi_tenor_explosion` fails because the multi-tenor input shape was wrong, adjust based on what `SHAPE_NOTES.md` (Task 1) shows the actual shape to be — update the test's payload AND the `_iter_pairs`/dict-handling code together.

- [ ] **Step 3: Reconcile KEY_MAP against `SHAPE_NOTES.md`**

Manually walk through `scripts/_seed_data/SHAPE_NOTES.md` row by row. For each `KEY_MAP_ADJUSTMENTS` entry:
- `RENAME` → update the dict key in `KEY_MAP`
- `MISSING` → comment out the entry, leave the comment explaining why
- `UNEXPECTED` → if it has a clear semantic match to one of the 13 charts, add a new `KEY_MAP` entry; otherwise note in `SHAPE_NOTES.md` and skip

After edits, re-run tests to confirm everything still passes.

```bash
pytest tests/test_seed_macro_monthly.py -v --no-cov 2>&1 | tail -10
```

- [ ] **Step 4: Commit (GREEN)**

```bash
git add scripts/seed_macro_monthly.py
git commit -m "feat(macro): KEY_MAP and pure row builders for seed script"
```

### Task 6: Add upsert + main CLI

**Files:**
- Modify: `scripts/seed_macro_monthly.py` (add `_upsert`, `main`, `__main__` guard)

- [ ] **Step 1: Add Supabase upsert helper and main entry**

Append to `scripts/seed_macro_monthly.py`:

```python
# ---------------------------------------------------------------------------
# Supabase upsert
# ---------------------------------------------------------------------------

_BATCH_SIZE = 500


def _resolve_credentials() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SERVICE_KEY")
    )
    if not url or not key:
        raise SystemExit(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in env"
        )
    return url.rstrip("/"), key


def _upsert(
    *,
    url: str,
    key: str,
    table: str,
    rows: list[dict],
    on_conflict: str,
    session: requests.Session | None = None,
) -> int:
    """Upsert rows in batches via PostgREST. Returns total rows sent."""
    if not rows:
        return 0
    sess = session or requests.Session()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    endpoint = f"{url}/rest/v1/{table}?on_conflict={on_conflict}"
    sent = 0
    for i in range(0, len(rows), _BATCH_SIZE):
        batch = rows[i : i + _BATCH_SIZE]
        resp = sess.post(endpoint, headers=headers, json=batch, timeout=60)
        if resp.status_code >= 300:
            raise RuntimeError(
                f"upsert {table} failed: HTTP {resp.status_code}: {resp.text[:500]}"
            )
        sent += len(batch)
    return sent


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_payload(refresh: bool) -> dict:
    """Load JSON from local cache, or fetch upstream if --refresh or cache missing."""
    if refresh or not LOCAL_CACHE.exists():
        logger.info("fetching upstream %s", UPSTREAM_URL)
        resp = requests.get(UPSTREAM_URL, timeout=30)
        resp.raise_for_status()
        LOCAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_CACHE.write_bytes(resp.content)
    return json.loads(LOCAL_CACHE.read_text())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="parse + transform; print summary; no Supabase writes")
    p.add_argument("--refresh", action="store_true",
                   help="re-fetch upstream JSON before transforming")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    payload = _load_payload(refresh=args.refresh)
    history_rows = build_history_rows(payload)
    definition_rows = build_definitions_rows()

    metric_ids = sorted({r["metric_id"] for r in history_rows})
    dates = [r["as_of"] for r in history_rows]
    summary = (
        f"prepared {len(history_rows)} history rows across {len(metric_ids)} metric_ids "
        f"(oldest={min(dates) if dates else '—'}, latest={max(dates) if dates else '—'}); "
        f"{len(definition_rows)} definition rows"
    )
    logger.info(summary)

    if args.dry_run:
        logger.info("--dry-run: no writes performed")
        return 0

    url, key = _resolve_credentials()
    sent_hist = _upsert(
        url=url, key=key,
        table="metric_history_monthly",
        rows=history_rows,
        on_conflict="metric_id,as_of",
    )
    sent_defs = _upsert(
        url=url, key=key,
        table="metric_definitions_monthly",
        rows=definition_rows,
        on_conflict="metric_id",
    )
    logger.info("upsert ok: %d history rows, %d definition rows", sent_hist, sent_defs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run dry-run against the local cache**

```bash
python -m scripts.seed_macro_monthly --dry-run --verbose 2>&1 | tail -10
```

Expected output (numbers will vary based on actual JSON):
```
... INFO: prepared 4760 history rows across 28 metric_ids (oldest=2012-01-01, latest=2026-04-01); 28 definition rows
... INFO: --dry-run: no writes performed
```

If the row count is wildly off (e.g. 0, or 100,000), STOP and inspect — the `KEY_MAP` likely has a structural mismatch with the actual JSON.

- [ ] **Step 3: Commit**

```bash
git add scripts/seed_macro_monthly.py
git commit -m "feat(macro): seed_macro_monthly upsert + CLI"
```

### Task 7: Add upsert tests

**Files:**
- Modify: `tests/test_seed_macro_monthly.py` (append new test class)

- [ ] **Step 1: Add upsert tests**

Append to `tests/test_seed_macro_monthly.py`:

```python
from unittest.mock import MagicMock

from scripts.seed_macro_monthly import _upsert


class TestUpsert:
    def _ok_session(self) -> MagicMock:
        sess = MagicMock()
        resp = MagicMock()
        resp.status_code = 201
        resp.text = ""
        sess.post.return_value = resp
        return sess

    def test_empty_rows_no_call(self):
        sess = self._ok_session()
        sent = _upsert(url="https://x.supabase.co", key="k",
                       table="t", rows=[], on_conflict="metric_id,as_of",
                       session=sess)
        assert sent == 0
        sess.post.assert_not_called()

    def test_single_batch_under_limit(self):
        sess = self._ok_session()
        rows = [{"metric_id": "x", "as_of": "2024-01-01", "value": 1.0,
                 "source": "macro_observer_seed", "source_as_of": "2024-01-01"}] * 10
        sent = _upsert(url="https://x.supabase.co", key="k",
                       table="metric_history_monthly", rows=rows,
                       on_conflict="metric_id,as_of", session=sess)
        assert sent == 10
        assert sess.post.call_count == 1
        endpoint = sess.post.call_args[0][0]
        assert "/rest/v1/metric_history_monthly?on_conflict=metric_id%2Cas_of" in endpoint \
            or "/rest/v1/metric_history_monthly?on_conflict=metric_id,as_of" in endpoint
        headers = sess.post.call_args[1]["headers"]
        assert headers["apikey"] == "k"
        assert "merge-duplicates" in headers["Prefer"]

    def test_multi_batch(self):
        sess = self._ok_session()
        # 1200 rows → 3 batches of 500/500/200 with _BATCH_SIZE=500
        rows = [{"metric_id": "x", "as_of": f"2024-{(i % 12) + 1:02d}-01",
                 "value": float(i), "source": "s", "source_as_of": "2024-01-01"}
                for i in range(1200)]
        sent = _upsert(url="https://x.supabase.co", key="k",
                       table="t", rows=rows, on_conflict="metric_id,as_of",
                       session=sess)
        assert sent == 1200
        assert sess.post.call_count == 3

    def test_non_2xx_raises(self):
        sess = MagicMock()
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "unauthorized"
        sess.post.return_value = resp
        with pytest.raises(RuntimeError, match="HTTP 401"):
            _upsert(url="https://x.supabase.co", key="bad",
                    table="t",
                    rows=[{"metric_id": "x", "as_of": "2024-01-01", "value": 1.0,
                           "source": "s", "source_as_of": "2024-01-01"}],
                    on_conflict="metric_id,as_of", session=sess)
```

- [ ] **Step 2: Run all seed tests**

```bash
pytest tests/test_seed_macro_monthly.py -v --no-cov 2>&1 | tail -20
```

Expected: all tests pass (existing + 4 new upsert tests).

- [ ] **Step 3: Run wider test suite to ensure no regressions**

```bash
pytest tests/ -q --no-cov 2>&1 | tail -10
```

Expected: same passing count as `main` HEAD plus the new test file's count. No regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/test_seed_macro_monthly.py
git commit -m "test(macro): upsert behavior tests for seed script"
```

### Task 8: Live seed run (REQUIRES USER APPROVAL)

- [ ] **Step 1: STOP and ask the user**

"Ready to run `python -m scripts.seed_macro_monthly` against live Supabase project `ssbliukchgibjcjohibi`. This will UPSERT ~4,800 rows into `metric_history_monthly` and ~28 rows into `metric_definitions_monthly`. Idempotent — re-runnable. Approve?"

- [ ] **Step 2: After approval, run live seed**

```bash
SUPABASE_URL="<from .env or shell>" \
SUPABASE_SERVICE_ROLE_KEY="<from .env or shell>" \
python -m scripts.seed_macro_monthly --verbose 2>&1 | tail -10
```

Expected: `upsert ok: NNNN history rows, NN definition rows`. Exit 0.

- [ ] **Step 3: Verify row counts in Supabase**

```
mcp__plugin_supabase_supabase__execute_sql
  project_id: ssbliukchgibjcjohibi
  query: select
           (select count(*) from metric_history_monthly) as hist_rows,
           (select count(distinct metric_id) from metric_history_monthly) as hist_metrics,
           (select min(as_of) from metric_history_monthly) as hist_oldest,
           (select max(as_of) from metric_history_monthly) as hist_latest,
           (select count(*) from metric_definitions_monthly) as def_rows;
```

Expected: `hist_rows` between 3,000–6,000; `hist_metrics` ≈ 28; `hist_oldest` ≈ `2012-01-01`; `hist_latest` recent (e.g. `2026-03-01` or `2026-04-01`); `def_rows` ≈ 28.

- [ ] **Step 4: Verify idempotency by re-running**

```bash
python -m scripts.seed_macro_monthly --verbose 2>&1 | tail -3
```

Then re-check counts via the same SQL — should be identical to Step 3 (no duplicate rows because of `unique (metric_id, as_of)` and `Prefer: resolution=merge-duplicates`).

---

## Phase 3 — Frontend Chart Configs

### Task 9: Write `chartConfigs.js` with 13 chart configs + 2 mini-chart configs

**Files:**
- Create: `pwa/pages/macro/chartConfigs.js`

- [ ] **Step 1: Write chart-config builders**

```javascript
// EconDelta /macro tab — Chart.js config builders.
//
// All functions are pure: they take (seriesByMetric: Record<string, Array<[date, value]>>)
// and return a Chart.js 4.4.0 config object. No DOM access, no side effects.
//
// Exposed via window.MACRO_CHART_CONFIGS (no ES modules — PWA uses inline Babel).

(function () {
  // Macro Observer-style canvas palette. Page chrome stays EconDelta's.
  const PALETTE = {
    primary: '#c8472b',
    primaryDim: 'rgba(200, 71, 43, 0.12)',
    secondary: '#2a8a59',
    accent: '#3b6ea5',
    grid: 'rgba(80, 60, 40, 0.10)',
    text: '#3d342a',
    cream: '#F4F1EA',
  };

  const FONT = { family: "'IBM Plex Serif', Georgia, serif" };

  // ---- helpers ----

  function toPoints(series) {
    return (series || []).map(([d, v]) => ({ x: d, y: v }));
  }

  function lastValue(series) {
    if (!series || !series.length) return null;
    return series[series.length - 1][1];
  }

  function baseLineOptions(opts = {}) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: !!opts.legend, position: 'top', labels: { font: FONT } },
        tooltip: { backgroundColor: PALETTE.text, titleFont: FONT, bodyFont: FONT },
      },
      scales: {
        x: {
          type: 'time',
          time: { unit: 'year', tooltipFormat: 'MMM yyyy' },
          grid: { color: PALETTE.grid },
          ticks: { color: PALETTE.text, font: FONT },
        },
        y: {
          grid: { color: PALETTE.grid },
          ticks: { color: PALETTE.text, font: FONT, ...(opts.yTicks || {}) },
        },
      },
    };
  }

  // Slice a series to ±N months around a focal date string 'YYYY-MM-01'.
  function windowAround(series, focalDate, monthsBeforeAfter) {
    if (!series) return [];
    const focal = new Date(focalDate);
    return series.filter(([d]) => {
      const dt = new Date(d);
      const diffMonths =
        (dt.getFullYear() - focal.getFullYear()) * 12 +
        (dt.getMonth() - focal.getMonth());
      return Math.abs(diffMonths) <= monthsBeforeAfter;
    });
  }

  // ---- 13 chart-config builders ----

  function cpiP2PConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'General', data: toPoints(s['point_to_point_inflation_monthly']),
            borderColor: PALETTE.primary, backgroundColor: PALETTE.primaryDim,
            borderWidth: 2, pointRadius: 0, tension: 0.2 },
          { label: 'Food', data: toPoints(s['cpi_p2p_food_monthly']),
            borderColor: PALETTE.secondary, borderWidth: 1.5, pointRadius: 0, tension: 0.2 },
          { label: 'Non-food', data: toPoints(s['cpi_p2p_nonfood_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0, tension: 0.2 },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  function inflation12mAvgConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: '12m avg', data: toPoints(s['cpi_12m_avg_monthly']),
            borderColor: PALETTE.primary, backgroundColor: PALETTE.primaryDim,
            borderWidth: 2, pointRadius: 0, tension: 0.2, fill: true },
        ],
      },
      options: baseLineOptions({ yTicks: { callback: v => v + '%' } }),
    };
  }

  function repoAndTbillConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'BB repo', data: toPoints(s['bb_repo_rate_monthly']),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 0 },
          { label: '364-day T-bill', data: toPoints(s['tbill_364d_yield_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0,
            borderDash: [4, 4] },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  // Yield curve: x-axis is tenor (numeric years), one dataset per as_of month.
  // Current month bold; priors at opacity 0.08.
  function yieldCurveConfig(s) {
    const tenors = [
      { id: 'yield_1y_monthly',  x: 1 },
      { id: 'yield_2y_monthly',  x: 2 },
      { id: 'yield_5y_monthly',  x: 5 },
      { id: 'yield_10y_monthly', x: 10 },
      { id: 'yield_20y_monthly', x: 20 },
    ];
    // Build {date: [{x,y}, ...]} grouped across tenors
    const byDate = {};
    tenors.forEach(t => {
      (s[t.id] || []).forEach(([d, v]) => {
        if (!byDate[d]) byDate[d] = [];
        byDate[d].push({ x: t.x, y: v });
      });
    });
    const dates = Object.keys(byDate).sort();
    if (!dates.length) return { type: 'line', data: { datasets: [] }, options: baseLineOptions() };
    const latest = dates[dates.length - 1];
    const datasets = dates.map(d => ({
      label: d,
      data: byDate[d].sort((a, b) => a.x - b.x),
      borderColor: d === latest ? PALETTE.primary : 'rgba(200,71,43,0.08)',
      borderWidth: d === latest ? 2.5 : 1,
      pointRadius: d === latest ? 3 : 0,
      tension: 0.1,
      showLine: true,
    }));
    return {
      type: 'line',
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        parsing: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { type: 'linear', title: { display: true, text: 'Tenor (years)', font: FONT },
               grid: { color: PALETTE.grid }, ticks: { color: PALETTE.text, font: FONT } },
          y: { ticks: { color: PALETTE.text, font: FONT, callback: v => v + '%' },
               grid: { color: PALETTE.grid } },
        },
      },
    };
  }

  function realPolicyRateConfig(s) {
    const data = (s['real_policy_rate_monthly'] || []).map(([d, v]) => ({ x: d, y: v }));
    return {
      type: 'bar',
      data: {
        datasets: [{
          label: 'Real policy rate',
          data,
          backgroundColor: ctx => ctx.parsed.y < 0 ? PALETTE.primary : PALETTE.secondary,
          borderWidth: 0,
        }],
      },
      options: baseLineOptions({ yTicks: { callback: v => v + '%' } }),
    };
  }

  function domesticCreditCompositionConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'Public', data: toPoints(s['domestic_credit_public_monthly']),
            borderColor: PALETTE.accent, backgroundColor: 'rgba(59,110,165,0.4)',
            fill: 'origin', borderWidth: 1.5, pointRadius: 0 },
          { label: 'Private', data: toPoints(s['domestic_credit_private_monthly']),
            borderColor: PALETTE.primary, backgroundColor: 'rgba(200,71,43,0.4)',
            fill: '-1', borderWidth: 1.5, pointRadius: 0 },
        ],
      },
      options: { ...baseLineOptions({ legend: true }),
                 scales: { ...baseLineOptions().scales, y: { ...baseLineOptions().scales.y, stacked: true } } },
    };
  }

  function domesticCreditGrowthConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'Public YoY', data: toPoints(s['public_credit_growth_yoy_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0 },
          { label: 'Private YoY', data: toPoints(s['private_credit_growth_yoy_monthly']),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 0 },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  function moneyGrowthConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'M1 YoY', data: toPoints(s['m1_growth_yoy_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0 },
          { label: 'M2 YoY', data: toPoints(s['m2_growth_yoy_monthly']),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 0 },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  function fxFlowsConfig(s) {
    // Symmetric: exports and remittance positive, imports negative
    const exp = toPoints(s['exports_usd_mn_monthly']);
    const rem = toPoints(s['remittance_usd_mn_monthly']);
    const imp = (s['imports_usd_mn_monthly'] || []).map(([d, v]) => ({ x: d, y: -Math.abs(v) }));
    return {
      type: 'bar',
      data: {
        datasets: [
          { label: 'Exports', data: exp, backgroundColor: PALETTE.secondary, borderWidth: 0, stack: 'inflow' },
          { label: 'Remittance', data: rem, backgroundColor: PALETTE.accent, borderWidth: 0, stack: 'inflow' },
          { label: 'Imports (–)', data: imp, backgroundColor: PALETTE.primary, borderWidth: 0, stack: 'outflow' },
        ],
      },
      options: { ...baseLineOptions({ legend: true }),
                 scales: { ...baseLineOptions().scales,
                           y: { ...baseLineOptions().scales.y, stacked: true },
                           x: { ...baseLineOptions().scales.x, stacked: true } } },
    };
  }

  function fxReservesConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [{
          label: 'Gross reserves',
          data: toPoints(s['gross_reserves_usd_bn_monthly']),
          borderColor: PALETTE.primary,
          backgroundColor: PALETTE.primaryDim,
          fill: 'origin',
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.2,
        }],
      },
      options: baseLineOptions({ yTicks: { callback: v => '$' + v + 'B' } }),
    };
  }

  function importCoverConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [{
          label: 'Import cover',
          data: toPoints(s['import_cover_months_monthly']),
          borderColor: PALETTE.primary,
          borderWidth: 2,
          pointRadius: 0,
        }],
      },
      options: {
        ...baseLineOptions({ yTicks: { callback: v => v + ' mo' } }),
        plugins: {
          ...baseLineOptions().plugins,
          // Threshold zone hints: <3 mo = stress, 3–6 mo = adequate, >6 mo = comfortable
          // (rendered in chart label, not annotation, to avoid plugin dep)
        },
      },
    };
  }

  function bdtUsdReerConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'BDT/USD (left)', data: toPoints(s['usd_bdt_mid_monthly']),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 0,
            yAxisID: 'y1' },
          { label: 'REER (right, 100=base)', data: toPoints(s['reer_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0,
            yAxisID: 'y2' },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: true, position: 'top', labels: { font: FONT } } },
        scales: {
          x: { type: 'time', time: { unit: 'year' }, grid: { color: PALETTE.grid },
               ticks: { color: PALETTE.text, font: FONT } },
          y1: { type: 'linear', position: 'left',  grid: { color: PALETTE.grid },
                ticks: { color: PALETTE.text, font: FONT } },
          y2: { type: 'linear', position: 'right', grid: { display: false },
                ticks: { color: PALETTE.text, font: FONT } },
        },
      },
    };
  }

  function dsexConfig(s, events) {
    const dataPts = toPoints(s['dsex_monthly']);
    const eventDots = (events || [])
      .filter(e => e.color && e.date)
      .map(e => {
        // Find DSEX value at event.date (within ±31d) for vertical placement
        const match = dataPts.find(p => p.x === e.date);
        return { x: e.date, y: match ? match.y : null, color: e.color, id: e.id };
      })
      .filter(p => p.y != null);

    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'DSEX', data: dataPts,
            borderColor: PALETTE.primary, backgroundColor: PALETTE.primaryDim,
            borderWidth: 2, pointRadius: 0, tension: 0.2, fill: true },
          { label: 'Events',
            data: eventDots,
            type: 'scatter',
            backgroundColor: eventDots.map(p => p.color),
            borderColor: '#fff',
            borderWidth: 1.5,
            pointRadius: 6,
            pointHoverRadius: 8,
            showLine: false },
        ],
      },
      options: baseLineOptions({ legend: false }),
    };
  }

  // ---- mini-charts for event modals ----

  function eventInflationRepoMiniConfig(s, eventDate) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'Inflation YoY', data: toPoints(windowAround(s['point_to_point_inflation_monthly'], eventDate, 6)),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 2 },
          { label: 'BB repo', data: toPoints(windowAround(s['bb_repo_rate_monthly'], eventDate, 6)),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 2, borderDash: [3, 3] },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  function eventReservesBdtMiniConfig(s, eventDate) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'Reserves (USD bn)',
            data: toPoints(windowAround(s['gross_reserves_usd_bn_monthly'], eventDate, 6)),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 2,
            yAxisID: 'y1' },
          { label: 'BDT/USD',
            data: toPoints(windowAround(s['usd_bdt_mid_monthly'], eventDate, 6)),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 2, borderDash: [3, 3],
            yAxisID: 'y2' },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: true, position: 'top', labels: { font: FONT } } },
        scales: {
          x: { type: 'time', time: { unit: 'month' }, grid: { color: PALETTE.grid },
               ticks: { color: PALETTE.text, font: FONT } },
          y1: { type: 'linear', position: 'left',  ticks: { color: PALETTE.text, font: FONT } },
          y2: { type: 'linear', position: 'right', grid: { display: false },
                ticks: { color: PALETTE.text, font: FONT } },
        },
      },
    };
  }

  // ---- registry exposed as global ----

  window.MACRO_CHART_CONFIGS = {
    PALETTE,
    lastValue,
    cpiP2P:                       cpiP2PConfig,
    inflation12mAvg:              inflation12mAvgConfig,
    repoAndTbill:                 repoAndTbillConfig,
    yieldCurve:                   yieldCurveConfig,
    realPolicyRate:               realPolicyRateConfig,
    domesticCreditComposition:    domesticCreditCompositionConfig,
    domesticCreditGrowth:         domesticCreditGrowthConfig,
    moneyGrowth:                  moneyGrowthConfig,
    fxFlows:                      fxFlowsConfig,
    fxReserves:                   fxReservesConfig,
    importCover:                  importCoverConfig,
    bdtUsdReer:                   bdtUsdReerConfig,
    dsex:                         dsexConfig,
    eventInflationRepoMini:       eventInflationRepoMiniConfig,
    eventReservesBdtMini:         eventReservesBdtMiniConfig,
  };
})();
```

- [ ] **Step 2: Quick syntax check via node**

```bash
node --check pwa/pages/macro/chartConfigs.js
```

Expected: no output (file is syntactically valid). If syntax error, fix before commit.

- [ ] **Step 3: Commit**

```bash
mkdir -p pwa/pages/macro
git add pwa/pages/macro/chartConfigs.js
git commit -m "feat(macro): chart-config builders (13 charts + 2 mini-charts)"
```

---

## Phase 4 — Events Seed

### Task 10: Write `events.js` with 11 entries

**Files:**
- Create: `pwa/pages/macro/events.js`

- [ ] **Step 1: Write events list**

```javascript
// EconDelta /macro tab — events seed.
// 11 events that map to colored dots on the DSEX chart and to modal cards.
// Exposed via window.MACRO_EVENTS (no ES modules — PWA uses inline Babel).
//
// Each event:
//   id           — stable string id
//   date         — first day of event month, "YYYY-MM-DD"
//   category     — short uppercase tag rendered in modal
//   title        — short headline
//   summary      — 1-line lede shown on the card
//   color        — dot color on DSEX chart
//   kpiMetricIds — 5 metric_ids surfaced as KPI rows in the modal

(function () {
  window.MACRO_EVENTS = [
    {
      id: 'aug12_dsex_birth',
      date: '2013-01-01',
      category: 'INDEX',
      title: 'DSEX Index Launches',
      summary: 'DSE adopts the broad-market DSEX as its primary benchmark.',
      color: '#3b6ea5',
      kpiMetricIds: [
        'dsex_monthly',
        'dsex_turnover_monthly',
        'point_to_point_inflation_monthly',
        'bb_repo_rate_monthly',
        'gross_reserves_usd_bn_monthly',
      ],
    },
    {
      id: 'jun18_us_taper',
      date: '2018-06-01',
      category: 'EXTERNAL',
      title: 'US Taper Tantrum · BDT Pressure',
      summary: 'Fed tightening + import surge drives BDT depreciation.',
      color: '#c8472b',
      kpiMetricIds: [
        'usd_bdt_mid_monthly',
        'gross_reserves_usd_bn_monthly',
        'reer_monthly',
        'imports_usd_mn_monthly',
        'remittance_usd_mn_monthly',
      ],
    },
    {
      id: 'mar20_covid',
      date: '2020-03-01',
      category: 'CRISIS',
      title: 'COVID-19 Lockdown',
      summary: 'Economic shutdown; remittance collapse; fiscal expansion.',
      color: '#8b1c0e',
      kpiMetricIds: [
        'point_to_point_inflation_monthly',
        'remittance_usd_mn_monthly',
        'gross_reserves_usd_bn_monthly',
        'm2_growth_yoy_monthly',
        'dsex_monthly',
      ],
    },
    {
      id: 'aug20_remittance_record',
      date: '2020-08-01',
      category: 'EXTERNAL',
      title: 'Remittance Surge',
      summary: 'Diaspora flows hit a multi-year peak through formal channels.',
      color: '#2a8a59',
      kpiMetricIds: [
        'remittance_usd_mn_monthly',
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'reer_monthly',
        'dsex_monthly',
      ],
    },
    {
      id: 'aug21_reserves_peak',
      date: '2021-08-01',
      category: 'EXTERNAL',
      title: 'FX Reserves Peak · $48bn',
      summary: 'Reserves crest before commodity-import shock begins.',
      color: '#2a8a59',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'import_cover_months_monthly',
        'imports_usd_mn_monthly',
        'usd_bdt_mid_monthly',
        'point_to_point_inflation_monthly',
      ],
    },
    {
      id: 'mar22_ukr_war',
      date: '2022-03-01',
      category: 'EXTERNAL',
      title: 'Russia–Ukraine War · Commodity Shock',
      summary: 'Energy and food import bills spike; pressure on reserves.',
      color: '#c8472b',
      kpiMetricIds: [
        'imports_usd_mn_monthly',
        'gross_reserves_usd_bn_monthly',
        'point_to_point_inflation_monthly',
        'usd_bdt_mid_monthly',
        'cpi_p2p_food_monthly',
      ],
    },
    {
      id: 'jul22_imf_call',
      date: '2022-07-01',
      category: 'POLICY',
      title: 'IMF Programme Discussions Begin',
      summary: 'Authorities engage IMF for $4.7B EFF/ECF/RSF support.',
      color: '#3b6ea5',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'import_cover_months_monthly',
        'usd_bdt_mid_monthly',
        'point_to_point_inflation_monthly',
        'bb_repo_rate_monthly',
      ],
    },
    {
      id: 'jan23_imf_disburse',
      date: '2023-02-01',
      category: 'POLICY',
      title: 'IMF First Disbursement',
      summary: 'First tranche under the $4.7B programme arrives.',
      color: '#2a8a59',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'reer_monthly',
        'bb_repo_rate_monthly',
        'point_to_point_inflation_monthly',
      ],
    },
    {
      id: 'aug24_smart_repeal',
      date: '2024-05-01',
      category: 'POLICY',
      title: 'SMART Lending-Cap Repealed',
      summary: 'BB shifts to corridor-based monetary policy; repo as anchor.',
      color: '#3b6ea5',
      kpiMetricIds: [
        'bb_repo_rate_monthly',
        'tbill_364d_yield_monthly',
        'private_credit_growth_yoy_monthly',
        'm2_growth_yoy_monthly',
        'point_to_point_inflation_monthly',
      ],
    },
    {
      id: 'aug24_transition',
      date: '2024-08-01',
      category: 'POLICY',
      title: 'Political Transition',
      summary: 'Interim administration takes office; reserves stabilise.',
      color: '#3b6ea5',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'dsex_monthly',
        'point_to_point_inflation_monthly',
        'bb_repo_rate_monthly',
      ],
    },
    {
      id: 'feb26_normalization',
      date: '2026-02-01',
      category: 'NORMALIZATION',
      title: 'Reserves Rebuild · Macro Stability',
      summary: 'FX reserves cross $35bn; inflation eases through 9%.',
      color: '#2a8a59',
      kpiMetricIds: [
        'point_to_point_inflation_monthly',
        'bb_repo_rate_monthly',
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'dsex_monthly',
      ],
    },
  ];
})();
```

- [ ] **Step 2: Syntax check**

```bash
node --check pwa/pages/macro/events.js
```

- [ ] **Step 3: Commit**

```bash
git add pwa/pages/macro/events.js
git commit -m "feat(macro): events seed (11 entries) with KPI metric_ids"
```

---

## Phase 5 — `macro.jsx` Page + Components

### Task 11: Write `macro.jsx`

**Files:**
- Create: `pwa/pages/macro.jsx`

- [ ] **Step 1: Write the page**

```jsx
// EconDelta /macro tab — long-horizon analytical view.
//
// Mounts on hash route '#/macro'. Lazy-loads Chart.js 4.4.0 from CDN on first
// visit (cached by service worker). Fetches metric_history_monthly via PostgREST
// using the same anon key already wired into pwa/lib/supabase-client.js.

const { useState: useStateM, useEffect: useEffectM, useMemo: useMemoM, useRef: useRefM } = React;

// ---------------------------------------------------------------------------
// Chart.js loader
// ---------------------------------------------------------------------------

const CHARTJS_URL = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js';
const CHARTJS_DATE_ADAPTER_URL =
  'https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js';

function ensureChartJS() {
  if (window.Chart && window.__edChartAdapterReady) return Promise.resolve(window.Chart);
  if (window.__edChartLoading) return window.__edChartLoading;

  const loadOne = (src) => new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src; s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('failed to load ' + src));
    document.head.appendChild(s);
  });

  window.__edChartLoading = (async () => {
    if (!window.Chart) await loadOne(CHARTJS_URL);
    if (!window.__edChartAdapterReady) {
      await loadOne(CHARTJS_DATE_ADAPTER_URL);
      window.__edChartAdapterReady = true;
    }
    return window.Chart;
  })();
  return window.__edChartLoading;
}

// ---------------------------------------------------------------------------
// Data fetcher — own PostgREST call so we don't bloat every page's bootstrap.
// ---------------------------------------------------------------------------

const KEY_METRICS_USED = [
  'point_to_point_inflation_monthly', 'cpi_p2p_food_monthly', 'cpi_p2p_nonfood_monthly',
  'cpi_12m_avg_monthly', 'bb_repo_rate_monthly', 'tbill_364d_yield_monthly',
  'yield_1y_monthly', 'yield_2y_monthly', 'yield_5y_monthly', 'yield_10y_monthly', 'yield_20y_monthly',
  'real_policy_rate_monthly',
  'domestic_credit_total_monthly', 'domestic_credit_public_monthly', 'domestic_credit_private_monthly',
  'private_credit_growth_yoy_monthly', 'public_credit_growth_yoy_monthly',
  'm1_growth_yoy_monthly', 'm2_growth_yoy_monthly',
  'exports_usd_mn_monthly', 'imports_usd_mn_monthly', 'remittance_usd_mn_monthly',
  'gross_reserves_usd_bn_monthly', 'import_cover_months_monthly',
  'usd_bdt_mid_monthly', 'reer_monthly',
  'dsex_monthly', 'dsex_turnover_monthly',
];

async function fetchMonthlyData() {
  if (window.ED_DATA && window.ED_DATA.macroMonthly) return window.ED_DATA.macroMonthly;
  const cfg = window.ED_SUPABASE_CONFIG;
  if (!cfg || !cfg.url || !cfg.anonKey) throw new Error('Supabase config missing');
  const inList = KEY_METRICS_USED.map(m => `"${m}"`).join(',');
  const url = `${cfg.url}/rest/v1/metric_history_monthly`
            + `?metric_id=in.(${inList})`
            + `&select=metric_id,as_of,value`
            + `&order=as_of.asc&limit=20000`;
  const resp = await fetch(url, {
    headers: { apikey: cfg.anonKey, Authorization: `Bearer ${cfg.anonKey}` },
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
  const rows = await resp.json();

  const byMetric = {};
  rows.forEach(r => {
    if (!byMetric[r.metric_id]) byMetric[r.metric_id] = [];
    byMetric[r.metric_id].push([r.as_of, Number(r.value)]);
  });
  if (!window.ED_DATA) window.ED_DATA = {};
  window.ED_DATA.macroMonthly = byMetric;
  return byMetric;
}

// ---------------------------------------------------------------------------
// ChartCard — one per chart, hosts a <canvas> and instantiates Chart.js
// ---------------------------------------------------------------------------

function ChartCard({ fig, title, subtitle, latestValueText, configFn, seriesByMetric, extra }) {
  const canvasRef = useRefM(null);
  const chartRef = useRefM(null);

  useEffectM(() => {
    let cancelled = false;
    ensureChartJS().then(Chart => {
      if (cancelled || !canvasRef.current) return;
      if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; }
      const cfg = configFn(seriesByMetric, extra);
      chartRef.current = new Chart(canvasRef.current.getContext('2d'), cfg);
    }).catch(err => {
      console.error('chart init failed', err);
    });
    return () => {
      cancelled = true;
      if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; }
    };
  }, [configFn, seriesByMetric, extra]);

  return (
    <div className="macro-card">
      <div className="macro-card-head">
        <span className="macro-fig">FIG.{String(fig).padStart(2, '0')}</span>
        <h3 className="macro-card-title">{title}</h3>
        {subtitle && <div className="macro-card-sub">{subtitle}</div>}
        {latestValueText && <div className="macro-card-latest">{latestValueText}</div>}
      </div>
      <div className="macro-card-canvas">
        <canvas ref={canvasRef}></canvas>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Event strip + modal
// ---------------------------------------------------------------------------

function EventStrip({ events, onSelect }) {
  return (
    <div className="macro-events">
      {events.map(e => (
        <button
          key={e.id}
          className="macro-event-card"
          style={{ borderLeftColor: e.color }}
          onClick={() => onSelect(e)}
        >
          <div className="macro-event-cat">{e.category}</div>
          <div className="macro-event-title">{e.title}</div>
          <div className="macro-event-date">{e.date}</div>
          <div className="macro-event-sum">{e.summary}</div>
        </button>
      ))}
    </div>
  );
}

function EventModal({ event, seriesByMetric, onClose }) {
  if (!event) return null;
  const cfgs = window.MACRO_CHART_CONFIGS;

  // KPI rows: pick the value at event.date (or nearest prior month)
  const kpiRows = (event.kpiMetricIds || []).map(mid => {
    const series = seriesByMetric[mid] || [];
    let val = null, asOf = null;
    for (let i = series.length - 1; i >= 0; i--) {
      if (series[i][0] <= event.date) { val = series[i][1]; asOf = series[i][0]; break; }
    }
    return { metricId: mid, value: val, asOf };
  });

  return (
    <div className="macro-modal-backdrop" onClick={onClose}>
      <div className="macro-modal" onClick={e => e.stopPropagation()}>
        <button className="macro-modal-close" onClick={onClose} aria-label="Close">×</button>
        <div className="macro-modal-cat">{event.category}</div>
        <h2 className="macro-modal-title">{event.title}</h2>
        <div className="macro-modal-date">{event.date}</div>

        <div className="macro-modal-kpis">
          {kpiRows.map(r => (
            <div key={r.metricId} className="macro-kpi-row">
              <span className="macro-kpi-label">{r.metricId}</span>
              <span className="macro-kpi-value">
                {r.value == null ? '—' : Number(r.value).toLocaleString()}
              </span>
              <span className="macro-kpi-date">{r.asOf || ''}</span>
            </div>
          ))}
        </div>

        <div className="macro-modal-charts">
          <ChartCard fig="A" title="Inflation & Repo (±6m)"
            configFn={cfgs.eventInflationRepoMini}
            seriesByMetric={seriesByMetric}
            extra={event.date}/>
          <ChartCard fig="B" title="Reserves & BDT/USD (±6m)"
            configFn={cfgs.eventReservesBdtMini}
            seriesByMetric={seriesByMetric}
            extra={event.date}/>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// PageMacro — root
// ---------------------------------------------------------------------------

function PageMacro() {
  const [data, setData] = useStateM(null);
  const [error, setError] = useStateM(null);
  const [openEvent, setOpenEvent] = useStateM(null);

  useEffectM(() => {
    fetchMonthlyData().then(setData).catch(e => setError(String(e)));
  }, []);

  const events = window.MACRO_EVENTS || [];
  const cfgs = window.MACRO_CHART_CONFIGS;

  if (error) {
    return (
      <React.Fragment>
        <PageHead kicker="Long-horizon · monthly observations" title="Macro"/>
        <p className="sec-lede" style={{ color: '#a33' }}>{error}</p>
      </React.Fragment>
    );
  }
  if (!data) {
    return (
      <React.Fragment>
        <PageHead kicker="Long-horizon · monthly observations" title="Macro"/>
        <div className="loading">loading monthly history…</div>
      </React.Fragment>
    );
  }

  // Latest-as-of for meta line
  const allDates = Object.values(data).flat().map(([d]) => d).sort();
  const latest = allDates.length ? allDates[allDates.length - 1] : '—';

  return (
    <React.Fragment>
      <PageHead
        kicker="Long-horizon · monthly observations"
        title="Macro"
        meta={`JAN 2012 — ${latest} · 13 charts · ${events.length} events`}
      />

      <section className="macro-section">
        <h2 className="macro-section-title">Prices &amp; Policy</h2>
        <div className="macro-grid">
          <ChartCard fig={4} title="CPI Inflation · Point-to-Point"
            subtitle="General, food, non-food YoY"
            configFn={cfgs.cpiP2P} seriesByMetric={data}/>
          <ChartCard fig={5} title="Inflation · 12-Month Average"
            configFn={cfgs.inflation12mAvg} seriesByMetric={data}/>
          <ChartCard fig={6} title="Repo &amp; 364-Day T-Bill"
            configFn={cfgs.repoAndTbill} seriesByMetric={data}/>
          <ChartCard fig={7} title="Sovereign Yield Curve · 1Y to 20Y"
            subtitle="One curve per month; latest highlighted"
            configFn={cfgs.yieldCurve} seriesByMetric={data}/>
          <ChartCard fig={8} title="Real Policy Rate"
            subtitle="Repo minus headline CPI"
            configFn={cfgs.realPolicyRate} seriesByMetric={data}/>
        </div>
      </section>

      <section className="macro-section">
        <h2 className="macro-section-title">Credit &amp; Money</h2>
        <div className="macro-grid">
          <ChartCard fig={1} title="Domestic Credit · Composition"
            subtitle="Public + private stack" configFn={cfgs.domesticCreditComposition}
            seriesByMetric={data}/>
          <ChartCard fig={2} title="Credit Growth · Public &amp; Private"
            configFn={cfgs.domesticCreditGrowth} seriesByMetric={data}/>
          <ChartCard fig={3} title="Money Growth · M1 &amp; M2"
            configFn={cfgs.moneyGrowth} seriesByMetric={data}/>
        </div>
      </section>

      <section className="macro-section">
        <h2 className="macro-section-title">External Sector</h2>
        <div className="macro-grid">
          <ChartCard fig={9} title="FX Inflows vs Outflows"
            subtitle="Exports + remittance vs imports"
            configFn={cfgs.fxFlows} seriesByMetric={data}/>
          <ChartCard fig={10} title="FX Reserves"
            configFn={cfgs.fxReserves} seriesByMetric={data}/>
          <ChartCard fig={11} title="Import Cover · Adequacy"
            subtitle="Months of imports covered by reserves"
            configFn={cfgs.importCover} seriesByMetric={data}/>
          <ChartCard fig={12} title="BDT/USD &amp; REER"
            configFn={cfgs.bdtUsdReer} seriesByMetric={data}/>
        </div>
      </section>

      <section className="macro-section">
        <h2 className="macro-section-title">Capital Market</h2>
        <div className="macro-grid">
          <ChartCard fig={13} title="DSEX Index · with event markers"
            configFn={cfgs.dsex} seriesByMetric={data} extra={events}/>
        </div>
        <EventStrip events={events} onSelect={setOpenEvent}/>
      </section>

      {openEvent && (
        <EventModal event={openEvent} seriesByMetric={data} onClose={() => setOpenEvent(null)}/>
      )}
    </React.Fragment>
  );
}

window.PageMacro = PageMacro;
```

- [ ] **Step 2: Quick syntax check (jsx via babel)**

```bash
node --check pwa/pages/macro.jsx 2>&1 || echo "(ok if only JSX-token errors — Babel transpiles in-browser)"
```

JSX won't pass `node --check` directly; the line above is a sanity check only. The browser will catch real syntax errors at load time.

- [ ] **Step 3: Append minimal CSS for `.macro-*` classes**

Modify `pwa/styles.css` — append at end:

```css
/* /macro tab */
.macro-section { margin: 32px 0; }
.macro-section-title {
  font-family: 'IBM Plex Serif', Georgia, serif;
  font-size: 1.25rem;
  font-weight: 600;
  margin: 0 0 16px;
  color: var(--color-text, #3d342a);
}
.macro-grid { display: grid; gap: 20px; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); }
.macro-card {
  background: var(--color-card, #fdfaf4);
  border: 1px solid rgba(80, 60, 40, 0.10);
  border-radius: 4px;
  padding: 16px;
  display: flex;
  flex-direction: column;
}
.macro-card-head { margin-bottom: 12px; }
.macro-fig {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.7rem;
  letter-spacing: 0.06em;
  color: #c8472b;
  font-weight: 600;
}
.macro-card-title {
  font-family: 'IBM Plex Serif', Georgia, serif;
  font-size: 1.05rem;
  font-weight: 600;
  margin: 4px 0 2px;
}
.macro-card-sub { font-size: 0.85rem; color: #6b5c4b; }
.macro-card-latest {
  font-size: 0.95rem;
  margin-top: 6px;
  font-weight: 600;
  color: #c8472b;
}
.macro-card-canvas { position: relative; height: 280px; }
@media (max-width: 720px) { .macro-card-canvas { height: 240px; } }

.macro-events {
  display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  margin-top: 16px;
}
.macro-event-card {
  background: var(--color-card, #fdfaf4);
  border: 1px solid rgba(80, 60, 40, 0.08);
  border-left-width: 4px;
  border-radius: 3px;
  padding: 12px 14px;
  cursor: pointer;
  text-align: left;
  font: inherit;
}
.macro-event-card:hover { background: #f8f3e8; }
.macro-event-cat {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.7rem;
  letter-spacing: 0.08em;
  color: #6b5c4b;
}
.macro-event-title { font-weight: 600; margin-top: 4px; }
.macro-event-date { font-size: 0.8rem; color: #8a7a68; margin-top: 2px; }
.macro-event-sum { font-size: 0.85rem; margin-top: 6px; line-height: 1.35; }

.macro-modal-backdrop {
  position: fixed; inset: 0; background: rgba(40, 30, 20, 0.55);
  display: flex; align-items: center; justify-content: center;
  z-index: 100; padding: 20px;
}
.macro-modal {
  background: var(--color-card, #fdfaf4);
  border-radius: 6px; padding: 28px; max-width: 760px; width: 100%;
  max-height: calc(100vh - 40px); overflow-y: auto; position: relative;
}
.macro-modal-close {
  position: absolute; top: 12px; right: 16px;
  background: none; border: none; font-size: 1.5rem; cursor: pointer; color: #6b5c4b;
}
.macro-modal-cat {
  font-family: 'IBM Plex Mono', monospace; font-size: 0.7rem;
  letter-spacing: 0.08em; color: #c8472b;
}
.macro-modal-title { margin: 4px 0; font-family: 'IBM Plex Serif', Georgia, serif; }
.macro-modal-date { font-size: 0.9rem; color: #8a7a68; margin-bottom: 16px; }
.macro-modal-kpis { display: grid; gap: 6px; margin-bottom: 20px; }
.macro-kpi-row {
  display: grid; grid-template-columns: 2fr 1fr 1fr;
  gap: 8px; font-size: 0.9rem; padding: 6px 0;
  border-bottom: 1px dashed rgba(80, 60, 40, 0.08);
}
.macro-kpi-label { font-family: 'IBM Plex Mono', monospace; color: #6b5c4b; }
.macro-kpi-value { text-align: right; font-weight: 600; }
.macro-kpi-date { text-align: right; color: #8a7a68; font-size: 0.8rem; }
.macro-modal-charts { display: grid; gap: 16px; }
@media (min-width: 720px) { .macro-modal-charts { grid-template-columns: 1fr 1fr; } }
```

- [ ] **Step 4: Commit**

```bash
git add pwa/pages/macro.jsx pwa/styles.css
git commit -m "feat(macro): page entry, ChartCard, EventStrip, EventModal + styles"
```

---

## Phase 6 — Wire Into PWA Shell

### Task 12: Update `pwa/index.html`

**Files:**
- Modify: `pwa/index.html`

- [ ] **Step 1: Add script tags + route handler**

Find the existing block of `<script type="text/babel" src="pages/...">` tags. **Insert two new script tags before `pages/sources-about.jsx`** (the chart configs and events must load before macro.jsx that consumes them):

```html
<script src="pages/macro/chartConfigs.js?v=__BUILD_VERSION__"></script>
<script src="pages/macro/events.js?v=__BUILD_VERSION__"></script>
<script type="text/babel" src="pages/macro.jsx?v=__BUILD_VERSION__"></script>
```

(Note: `chartConfigs.js` and `events.js` are plain JS, no `text/babel`. `macro.jsx` is JSX so it gets `text/babel`.)

Then in the route block (around line 87 of index.html based on earlier inspection), add:

```jsx
else if(route === '/macro'){ Page = PageMacro; label = '02 Macro'; }
```

between the `'/archive'` and `'/runs'` branches. Re-number `'/archive'` → `'01b Archive'`? **No** — keep existing labels stable; just add `'02 Macro'` and let other labels stay. Actually the labels come from `data-screen-label` for analytics; not user-visible — pick any unique label. Use:

```jsx
else if(route === '/macro')  { Page = PageMacro;  label = '02 Macro'; }
```

(The full updated route block:

```jsx
let Page, label;
if(route === '/archive'){ Page = PageArchive; label = '02 Archive'; }
else if(route === '/macro')  { Page = PageMacro;   label = '06 Macro'; }
else if(route === '/runs'){ Page = PageRuns; label = '03 Runs'; }
else if(route === '/sources'){ Page = PageSources; label = '04 Sources'; }
else if(route === '/about'){ Page = PageAbout; label = '05 About'; }
else { Page = PageLatest; label = '01 Latest'; }
```
)

- [ ] **Step 2: Sanity check the file**

```bash
grep -c "pages/macro" pwa/index.html
grep -c "PageMacro" pwa/index.html
```

Expected: `pages/macro` ≥ 3 (chartConfigs, events, macro.jsx); `PageMacro` ≥ 1 (route handler).

- [ ] **Step 3: Commit**

```bash
git add pwa/index.html
git commit -m "feat(macro): wire macro page into index.html route table"
```

### Task 13: Update `pwa/components.jsx` nav

**Files:**
- Modify: `pwa/components.jsx`

- [ ] **Step 1: Add Macro entry to nav `items` array**

Edit the `items` array (around line 69–79). Replace the array with:

```jsx
const items = [
  { group: 'Pipeline', links: [
    { href:'#/latest',  label:'Latest',   badge:'live', icon:'◐' },
    { href:'#/archive', label:'Archive',  badge:'90d',  icon:'◫' },
    { href:'#/runs',    label:'Run dashboard', badge:null, icon:'▦' },
  ]},
  { group: 'Analysis', links: [
    { href:'#/macro',   label:'Macro',    badge:'14y',  icon:'≋' },
  ]},
  { group: 'Reference', links: [
    { href:'#/sources', label:'Sources',  badge:'4',  icon:'◆' },
    { href:'#/about',   label:'About',    badge:null, icon:'§' },
  ]},
];
```

- [ ] **Step 2: Sanity check**

```bash
grep -A1 "'Analysis'" pwa/components.jsx
```

Expected: shows the new group with `#/macro` href.

- [ ] **Step 3: Commit**

```bash
git add pwa/components.jsx
git commit -m "feat(macro): add Macro nav entry under Analysis group"
```

### Task 14: Update `pwa/sw.js` cache list

**Files:**
- Modify: `pwa/sw.js`

- [ ] **Step 1: Bump cache name and add new files**

Edit:
- Line 9: change `'econdelta-v1-2026-05-04'` → `'econdelta-v1-2026-05-05-macro'`
- `APP_SHELL` array: append three new entries

After edit, the relevant block reads:

```javascript
const CACHE_NAME = 'econdelta-v1-2026-05-05-macro';
const VENDOR_CACHE = 'econdelta-vendor-v1';
const RPC_CACHE = 'econdelta-rpc-v1';

const APP_SHELL = [
  './',
  './index.html',
  './styles.css',
  './manifest.webmanifest',
  './config.js',
  './lib/supabase-client.js',
  './components.jsx',
  './pages/latest.jsx',
  './pages/archive.jsx',
  './pages/runs.jsx',
  './pages/sources-about.jsx',
  './pages/macro.jsx',
  './pages/macro/chartConfigs.js',
  './pages/macro/events.js',
  './icons/icon-192.png',
  './icons/icon-512.png',
];
```

(Chart.js CDN URL is intentionally NOT pre-cached at install — service worker's stale-while-revalidate tier 2 will cache it on first fetch.)

- [ ] **Step 2: Sanity check**

```bash
grep "macro" pwa/sw.js
```

Expected: 3 lines (`./pages/macro.jsx`, `./pages/macro/chartConfigs.js`, `./pages/macro/events.js`).

- [ ] **Step 3: Commit**

```bash
git add pwa/sw.js
git commit -m "feat(macro): cache /macro page assets in service worker"
```

---

## Phase 7 — Smoke + Ship

### Task 15: Local smoke test

- [ ] **Step 1: Start a static server**

```bash
cd pwa && python3 -m http.server 8765 &
SERVER_PID=$!
sleep 2
```

- [ ] **Step 2: Open browser to `/macro` route**

Manually open in browser: `http://localhost:8765/#/macro`

Expected:
1. Page loads; sidebar shows new `Macro` entry under `Analysis` group; clicking it activates the page.
2. After ~1–2s (Chart.js download), 13 chart canvases appear across 4 sections.
3. EventStrip shows 11 cards under the DSEX section.
4. Clicking an event card opens the modal with 5 KPI rows + 2 mini-charts.
5. ESC or backdrop click closes the modal.
6. No errors in the browser console.

If charts are empty or distorted, common issues:
- Chart.js date adapter missing → check that `chartjs-adapter-date-fns` `<script>` loads (the `ensureChartJS` function loads it).
- KEY_MAP mismatch → re-check that `KEY_METRICS_USED` in `macro.jsx` matches the `metric_id` strings produced by the seed script.
- CORS on Supabase fetch → confirm `ED_SUPABASE_CONFIG` is set in `pwa/config.js`.

- [ ] **Step 3: Other tabs still work**

Click each of: `Latest`, `Archive`, `Run dashboard`, `Sources`, `About`. Each should render unchanged. No regressions.

- [ ] **Step 4: Stop the static server**

```bash
kill $SERVER_PID 2>/dev/null || true
```

- [ ] **Step 5: Commit any visual fixes**

If you tweaked CSS or chart configs based on smoke-test findings, commit those:

```bash
git add -A
git commit -m "fix(macro): visual polish from local smoke test"
```

(Skip this commit if no fixes needed.)

### Task 16: Open PR

- [ ] **Step 1: Push branch**

```bash
git push -u origin feat/macro-tab
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --base main --head feat/macro-tab \
  --title "feat: /macro long-horizon analytical tab" \
  --body "$(cat <<'EOF'
## Summary

- Adds new `/macro` tab to the EconDelta PWA with 13 long-horizon monthly charts (Jan 2012 → latest) plus 11 click-to-open event modals
- Mines monthly history from Macro Observer's public JSON into two new Supabase tables (`metric_history_monthly`, `metric_definitions_monthly`)
- Existing `/latest`, `/archive`, `/runs`, `/sources`, `/about` tabs untouched — daily scrapers, parsers, and aggregator unchanged

## Architecture

- One-shot Python ingest: `scripts/seed_macro_monthly.py` (idempotent, fully tested)
- Two new migrations: `0006_metric_history_monthly.sql`, `0007_metric_definitions_monthly.sql`
- Frontend: `pwa/pages/macro.jsx` + `pwa/pages/macro/{chartConfigs,events}.js`
- Chart.js 4.4.0 lazy-loaded from CDN on first `/macro` visit (cached by SW)

## Spec & Plan

- Design spec: `docs/superpowers/specs/2026-05-05-macro-tab-long-horizon-design.md`
- Implementation plan: `docs/superpowers/plans/2026-05-05-macro-tab-implementation.md`

## Test plan

- [x] `pytest tests/test_seed_macro_monthly.py -v` — all pass
- [x] `pytest tests/ -q` — no regressions vs main
- [x] `python -m scripts.seed_macro_monthly --dry-run` — produces expected row counts
- [x] Live seed run upserted ~4,800 history rows + ~28 definitions
- [x] `/macro` renders 13 charts in browser, all data populated
- [x] EventStrip + EventModal render and close correctly
- [x] `/latest`, `/archive`, `/runs`, `/sources`, `/about` unchanged
- [ ] Mobile/tablet smoke (real device)
- [ ] Tomorrow's cron cascade (2026-05-06 05:00–05:20 BDT) runs clean — confirms zero regression on the operational pipeline
EOF
)"
```

Expected: PR URL printed.

- [ ] **Step 3: Wait for CI green**

```bash
gh pr checks
```

If checks fail, address the failures and push fixes; do not merge red.

### Task 17: Merge PR (REQUIRES USER APPROVAL)

- [ ] **Step 1: STOP and ask the user**

"PR ready to merge: <URL>. CI is green. Approve `gh pr merge <N> --merge --delete-branch`?"

- [ ] **Step 2: After approval, merge**

```bash
gh pr merge <PR_NUMBER> --merge --delete-branch
```

- [ ] **Step 3: Verify deploy**

The PWA deploys from `main` via the existing GitHub Actions workflow. Wait ~2–3 min, then:

```bash
curl -s https://econdelta.clauding-lab.com/ | grep -c "pages/macro"
```

Expected: ≥ 3 (the three new script references in index.html).

Visit `https://econdelta.clauding-lab.com/#/macro` in a browser; confirm the new tab renders the same as it did locally.

---

## Cross-Cutting Reminders

### What this plan does NOT do

- No changes to `metric_history`, `metric_definitions`, scrapers, parsers, aggregators
- No new cron timers, no new systemd units
- No primary-source extraction (BB / BBS / DSE) — explicitly deferred per spec
- No Macro Temperature gauge, driver cards, NOTE blocks, or event narratives — explicitly excluded per spec
- No daily-and-monthly unified views — explicitly out of scope per spec

### What to watch tomorrow morning (2026-05-06)

The cron cascade fires 05:00–05:20 BDT. Visit `https://econdelta.clauding-lab.com/#/runs` after 05:30 BDT. Expected: all 6 services `status=ok`. If anything red, the `/macro` tab work is unrelated — `/macro` is read-only against new tables that the operational pipeline never touches. But check anyway because deploys can interact with caching in subtle ways.

---

## Self-Review Checklist (run before handing off)

**Spec coverage:**
- [x] D1 (new tab, /archive untouched) — Tasks 12, 13
- [x] D2 (Macro Observer JSON seed) — Task 1, 5, 6, 8
- [x] D3 (13 charts, no Macro Temp / driver cards / NOTE) — Task 9 (only 13 + 2 mini configs); Task 11 (no narrative in EventModal)
- [x] D4 (separate `metric_history_monthly`, `metric_definitions_monthly`) — Tasks 2, 3
- [x] D5 (Macro Observer styling inside canvas only) — Task 9 (`PALETTE`, `FONT` confined to chart configs)
- [x] D6 (Chart.js 4.4.0 lazy-loaded) — Task 11 (`ensureChartJS`)
- [x] Schema spec — Tasks 2, 3 (matches spec lines 199–253)
- [x] KEY_MAP — Task 5 (full set)
- [x] DSEX event-dot annotation — Task 9 (`dsexConfig` second arg)
- [x] EventModal with 5 KPIs + 2 mini-charts, no narrative — Task 11
- [x] PageMacro with 4 sections — Task 11
- [x] Lazy-load + SW caching — Task 11, 14
- [x] Tests for ingestion — Tasks 4, 7
- [x] Error handling (empty data, schema break, Chart.js fail) — Task 11 (loading/error states), Task 5 (warn-on-unknown-key)

**Placeholder scan:**
- [x] No "TBD" / "TODO" / "implement later" in any code block — all complete
- [x] No "similar to Task N" without repeating the code
- [x] All `<PR_NUMBER>` and `<URL>` are explicitly marked as placeholders that the implementer fills in at runtime

**Type/name consistency:**
- [x] `metric_history_monthly` (snake_case) used everywhere — schema, script, frontend fetch URL
- [x] `KEY_METRICS_USED` in macro.jsx matches `metric_id` values produced by `KEY_MAP` in seed script (28 entries)
- [x] `MetricMap` dataclass shape consistent: `metric_id, display_name, unit, domain, notes`
- [x] Migration filenames match what the SQL apply step uses
- [x] `window.MACRO_CHART_CONFIGS` keys used by `macro.jsx` (cpiP2P, inflation12mAvg, repoAndTbill, yieldCurve, realPolicyRate, domesticCreditComposition, domesticCreditGrowth, moneyGrowth, fxFlows, fxReserves, importCover, bdtUsdReer, dsex, eventInflationRepoMini, eventReservesBdtMini) match those exposed by `chartConfigs.js`
- [x] `window.MACRO_EVENTS` shape consumed by EventStrip + EventModal matches `events.js` definition (`id, date, category, title, summary, color, kpiMetricIds`)

**End of plan.**
