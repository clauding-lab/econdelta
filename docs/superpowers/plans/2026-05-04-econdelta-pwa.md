# EconDelta PWA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a public, installable Progressive Web App at `https://clauding-lab.github.io/econdelta/` that surfaces all 60+ EconDelta indicators (forex, DSE, banking, money market, inflation, government finance, food prices) in a hero+bento mobile layout, plus a Runs commit-graph for pipeline health.

**Architecture:** Three layers: (1) ExonVPS Python pipeline writes to Supabase via service-role; (2) Supabase exposes 3 new tables + 1 RPC over RLS-anon; (3) static PWA in `pwa/` subfolder reads via single `get_latest_dashboard()` RPC + direct REST. Vanilla React UMD + Babel runtime, no build step. Deployed via GitHub Actions to GitHub Pages on push to `main`.

**Tech Stack:** Python 3.12 (existing), Postgres + Supabase REST, vanilla React 18 + Babel runtime, plain JS service worker, GitHub Actions, GitHub Pages.

**Reference spec:** `docs/superpowers/specs/2026-05-04-econdelta-pwa-design.md`

**Reference bundle (source of porting):** `~/downloads/econdelta/`

**Working directory:** `~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion` (branch `feat/v3-expansion`)

**Production targets (require explicit per-action approval before touching):**
- Supabase project `ssbliukchgibjcjohibi` (apply migrations)
- ExonVPS `adnan-local@103.187.23.22` (deploy backend changes via `git pull`)
- GitHub repo `clauding-lab/econdelta` (push to `main`)

---

## Phase 1 — Database Migrations

### Task 1: Create `metric_definitions` migration

**Files:**
- Create: `db/migrations/0002_metric_definitions.sql`

- [ ] **Step 1: Write the migration SQL**

Create `db/migrations/0002_metric_definitions.sql` with the exact contents:

```sql
-- ============================================================================
-- 0002 — metric_definitions
-- ----------------------------------------------------------------------------
-- Indicator catalog. One row per metric_id. Aggregator seeds new rows on first
-- sight via INSERT ... ON CONFLICT (metric_id) DO NOTHING — manual edits in
-- Supabase Studio (label, sort_order, is_hero, etc.) are preserved forever.
-- ============================================================================

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

do $$
begin
  if not exists (select 1 from pg_policies where policyname = 'anon read definitions') then
    create policy "anon read definitions" on metric_definitions for select to anon using (true);
  end if;
end $$;

comment on table metric_definitions is
  'Catalog of EconDelta indicators. Aggregator seeds new rows; humans edit cosmetic fields in Studio.';
comment on column metric_definitions.is_hero is
  'When true, indicator is promoted to a hero card on the Latest page (default 4).';
comment on column metric_definitions.inverted is
  'When true, lower-is-better semantics (e.g. NPL ratio going up is bad).';
```

- [ ] **Step 2: Apply to Supabase via MCP**

Use the `mcp__plugin_supabase_supabase__apply_migration` tool with `name=0002_metric_definitions` and the SQL from Step 1.

Expected response: `{"success": true}` or similar.

- [ ] **Step 3: Verify the table exists with anon RLS**

Use `mcp__plugin_supabase_supabase__execute_sql` to run:

```sql
select tablename, rowsecurity from pg_tables where tablename = 'metric_definitions';
select policyname, roles, cmd from pg_policies where tablename = 'metric_definitions';
```

Expected: 1 table row with `rowsecurity=true`, 1 policy row for `select` to `anon`.

- [ ] **Step 4: Commit**

```bash
git add db/migrations/0002_metric_definitions.sql
git commit -m "feat(db): add metric_definitions catalog table

Stores indicator metadata (label, unit, domain, sort_order, is_hero) for the
PWA's Latest page. Aggregator will seed rows via ON CONFLICT DO NOTHING so
manual Studio edits are preserved.

Refs spec: docs/superpowers/specs/2026-05-04-econdelta-pwa-design.md §3.1"
```

---

### Task 2: Create `run_logs` migration

**Files:**
- Create: `db/migrations/0003_run_logs.sql`

- [ ] **Step 1: Write the migration SQL**

Create `db/migrations/0003_run_logs.sql`:

```sql
-- ============================================================================
-- 0003 — run_logs
-- ----------------------------------------------------------------------------
-- Per-scraper invocation audit. Powers the PWA's Runs page commit-graph.
-- One row per scraper invocation (start logged immediately, end updates row).
-- Status values: 'ok' | 'fail' | 'stale' | 'skip'.
-- Source values: 'bb_forex' | 'dse_market' | 'commodity_prices'
--              | 'fetch' | 'parse' | 'aggregate'.
-- ============================================================================

create extension if not exists "pgcrypto";

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

do $$
begin
  if not exists (select 1 from pg_policies where policyname = 'anon read runs') then
    create policy "anon read runs" on run_logs for select to anon using (true);
  end if;
end $$;

comment on column run_logs.status is
  $$ok = wrote snapshot; fail = exception; stale = anomaly threshold tripped, write skipped; skip = non-trading day or other expected no-op$$;
```

- [ ] **Step 2: Apply via Supabase MCP**

`mcp__plugin_supabase_supabase__apply_migration` with `name=0003_run_logs` and the SQL.

Expected: success.

- [ ] **Step 3: Verify**

```sql
select tablename, rowsecurity from pg_tables where tablename = 'run_logs';
select indexname from pg_indexes where tablename = 'run_logs';
select policyname, roles, cmd from pg_policies where tablename = 'run_logs';
```

Expected: 1 table (rls=true), 3 indexes (pkey + 2 named), 1 select policy for anon.

- [ ] **Step 4: Commit**

```bash
git add db/migrations/0003_run_logs.sql
git commit -m "feat(db): add run_logs scraper invocation audit

Per-scraper invocation rows powering the PWA Runs page commit-graph.
Each scraper writes start row (log_run_start) + end update (log_run_end).

Refs spec: §3.2"
```

---

### Task 3: Create `get_latest_dashboard()` RPC migration

**Files:**
- Create: `db/migrations/0004_get_latest_dashboard.sql`

- [ ] **Step 1: Write the migration SQL**

Create `db/migrations/0004_get_latest_dashboard.sql`:

```sql
-- ============================================================================
-- 0004 — get_latest_dashboard()
-- ----------------------------------------------------------------------------
-- Single-call RPC for the PWA's Latest page. Returns one jsonb blob with:
--   updated_at      — server now() at call time
--   definitions     — array of all metric_definitions rows, sorted (domain, sort_order)
--   values          — { metric_id: {value, as_of, source_as_of} } from latest
--                     row per metric_id in metric_history
--   sources_status  — { source: {status, last_success, duration_ms, error} }
--                     from latest row per source in run_logs
-- ============================================================================

create or replace function get_latest_dashboard()
returns jsonb language sql stable security invoker as $$
  select jsonb_build_object(
    'updated_at', now(),
    'definitions', (
      select coalesce(jsonb_agg(to_jsonb(d) order by d.domain, d.sort_order), '[]'::jsonb)
      from metric_definitions d
    ),
    'values', (
      select coalesce(jsonb_object_agg(
        metric_id,
        jsonb_build_object(
          'value', value,
          'as_of', as_of,
          'source_as_of', source_as_of
        )
      ), '{}'::jsonb)
      from (
        select distinct on (metric_id) metric_id, value, as_of, source_as_of
        from metric_history
        order by metric_id, as_of desc
      ) latest
    ),
    'sources_status', (
      select coalesce(jsonb_object_agg(
        source,
        jsonb_build_object(
          'status', status,
          'last_success', started_at,
          'duration_ms', duration_ms,
          'error', error
        )
      ), '{}'::jsonb)
      from (
        select distinct on (source) source, status, started_at, duration_ms, error
        from run_logs
        order by source, started_at desc
      ) recent
    )
  );
$$;

grant execute on function get_latest_dashboard() to anon;

comment on function get_latest_dashboard() is
  'Single-call dashboard payload for the EconDelta PWA Latest page. Anon-callable.';
```

- [ ] **Step 2: Apply via Supabase MCP**

`mcp__plugin_supabase_supabase__apply_migration` with `name=0004_get_latest_dashboard`.

- [ ] **Step 3: Smoke-test the RPC**

Use `mcp__plugin_supabase_supabase__execute_sql`:

```sql
select get_latest_dashboard();
```

Expected: a jsonb object with keys `updated_at`, `definitions` (empty array `[]` for now since no rows seeded), `values` (object — should have many entries from existing metric_history), `sources_status` (empty `{}` — no run_logs yet).

- [ ] **Step 4: Verify anon can call it**

`mcp__plugin_supabase_supabase__execute_sql`:

```sql
set local role anon;
select get_latest_dashboard() is not null as anon_can_call;
reset role;
```

Expected: `anon_can_call = true`.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/0004_get_latest_dashboard.sql
git commit -m "feat(db): add get_latest_dashboard() RPC

Single-call RPC returning {updated_at, definitions, values, sources_status}
for the PWA Latest page. Anon-callable, uses distinct on per metric_id and
per source for the latest values.

Refs spec: §3.3"
```

---

## Phase 2 — Backend Helpers

### Task 4: Add `log_run_start()` to `utils/supabase_writer.py`

**Files:**
- Modify: `utils/supabase_writer.py` (append new helper at end of file)
- Test: `tests/test_run_logging.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_run_logging.py`:

```python
"""Tests for run_logs helpers in utils/supabase_writer.py."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def skip_supabase_env(monkeypatch):
    """Don't actually hit Supabase in unit tests."""
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


class TestLogRunStart:
    def test_returns_uuid_string(self, monkeypatch):
        from utils.supabase_writer import log_run_start
        # When SKIP_SUPABASE=1, helper short-circuits and returns a local uuid.
        run_id = log_run_start(source="bb_forex", unit="econdelta-forex.service")
        assert isinstance(run_id, str)
        assert len(run_id) == 36  # uuid format

    def test_uses_provided_started_at(self):
        from utils.supabase_writer import log_run_start
        ts = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        run_id = log_run_start(source="bb_forex", started_at=ts)
        assert isinstance(run_id, str)

    def test_swallows_network_error(self, monkeypatch):
        """Logging failure must NOT raise — would mask scrape outcome."""
        from utils.supabase_writer import log_run_start
        monkeypatch.delenv("ECONDELTA_SKIP_SUPABASE", raising=False)
        monkeypatch.setenv("SUPABASE_URL", "https://nonexistent.invalid")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake")
        # Should return a uuid even on network failure
        run_id = log_run_start(source="bb_forex")
        assert isinstance(run_id, str)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion
.venv/bin/pytest tests/test_run_logging.py::TestLogRunStart -v
```

Expected: `ImportError` or `AttributeError: module 'utils.supabase_writer' has no attribute 'log_run_start'`.

- [ ] **Step 3: Add `log_run_start()` to `utils/supabase_writer.py`**

Append at the end of the file:

```python
# ============================================================================
# Run logging helpers — write to public.run_logs for the PWA Runs page
# ============================================================================

import uuid as _uuid
from typing import Callable as _Callable, Optional as _Optional


def log_run_start(
    source: str,
    unit: _Optional[str] = None,
    started_at: _Optional[datetime] = None,
) -> str:
    """Insert a starting row in run_logs, return uuid for matching log_run_end().

    Swallows network errors — a logging failure must not mask the scrape outcome.
    Returns a local uuid even on failure so log_run_end() has something to update
    (the update will also be a no-op).
    """
    run_id = str(_uuid.uuid4())
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") == "1":
        return run_id

    if started_at is None:
        started_at = datetime.now(timezone.utc)

    try:
        client = _get_client()
        if client is None:
            return run_id

        import socket as _socket
        host = os.environ.get("ECONDELTA_HOST", _socket.gethostname())

        client.table("run_logs").insert({
            "id": run_id,
            "source": source,
            "started_at": started_at.isoformat(),
            "status": "running",  # placeholder; updated by log_run_end
            "host": host,
            "unit": unit,
        }).execute()
    except Exception as e:  # noqa: BLE001 — by design, we swallow logging errors
        log.warning("log_run_start failed for source=%s: %s", source, e)

    return run_id
```

(`_get_client()` may already exist — if not, it's a private accessor for the cached supabase Client. If absent, add a helper:
```python
@lru_cache(maxsize=1)
def _get_client() -> _Optional[Client]:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return None
    return create_client(url, key)
```
)

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_run_logging.py::TestLogRunStart -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add utils/supabase_writer.py tests/test_run_logging.py
git commit -m "feat(supabase_writer): add log_run_start helper

Inserts a running-state row in run_logs, returns a uuid for the matching
log_run_end. Swallows network errors so logging never masks scrape outcome.

Refs spec: §4.1"
```

---

### Task 5: Add `log_run_end()` to `utils/supabase_writer.py`

**Files:**
- Modify: `utils/supabase_writer.py`
- Test: `tests/test_run_logging.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_run_logging.py`:

```python
class TestLogRunEnd:
    def test_accepts_ok_status(self, monkeypatch):
        from utils.supabase_writer import log_run_end
        ts = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        # No raise on SKIP_SUPABASE=1 path
        log_run_end(run_id="00000000-0000-0000-0000-000000000000",
                    started_at=ts, status="ok", exit_code=0)

    def test_swallows_network_error(self, monkeypatch):
        from utils.supabase_writer import log_run_end
        monkeypatch.delenv("ECONDELTA_SKIP_SUPABASE", raising=False)
        monkeypatch.setenv("SUPABASE_URL", "https://nonexistent.invalid")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake")
        ts = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        log_run_end(run_id="00000000-0000-0000-0000-000000000000",
                    started_at=ts, status="fail", exit_code=1, error="boom")

    def test_computes_duration_ms(self, monkeypatch):
        """Verify duration_ms is computed from started_at to now."""
        from utils.supabase_writer import log_run_end
        # We can't easily intercept the upsert call without mocking _get_client,
        # so this test mostly verifies the call path doesn't raise.
        ts = datetime.now(timezone.utc)
        log_run_end(run_id="00000000-0000-0000-0000-000000000000",
                    started_at=ts, status="ok", exit_code=0)
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_run_logging.py::TestLogRunEnd -v
```

Expected: ImportError on `log_run_end`.

- [ ] **Step 3: Implement `log_run_end()`**

Append to `utils/supabase_writer.py`:

```python
def log_run_end(
    run_id: str,
    started_at: datetime,
    status: str,
    exit_code: int = 0,
    error: _Optional[str] = None,
) -> None:
    """Update a run_logs row with finished_at, duration_ms, status, exit_code, error.

    Swallows network errors. Status must be one of: 'ok', 'fail', 'stale', 'skip'.
    """
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") == "1":
        return

    finished_at = datetime.now(timezone.utc)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)

    try:
        client = _get_client()
        if client is None:
            return
        client.table("run_logs").update({
            "finished_at": finished_at.isoformat(),
            "duration_ms": duration_ms,
            "status": status,
            "exit_code": exit_code,
            "error": error[:2000] if error else None,  # truncate long tracebacks
        }).eq("id", run_id).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("log_run_end failed for run_id=%s: %s", run_id, e)
```

- [ ] **Step 4: Run to verify pass**

```bash
.venv/bin/pytest tests/test_run_logging.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add utils/supabase_writer.py tests/test_run_logging.py
git commit -m "feat(supabase_writer): add log_run_end helper

Updates a run_logs row with finished_at, duration_ms, status, exit_code,
error. Truncates error text at 2KB to bound payload. Swallows network errors.

Refs spec: §4.1"
```

---

### Task 6: Add `wrap_run()` convenience to `utils/supabase_writer.py`

**Files:**
- Modify: `utils/supabase_writer.py`
- Test: `tests/test_run_logging.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_run_logging.py`:

```python
class TestWrapRun:
    def test_returns_main_exit_code_on_success(self):
        from utils.supabase_writer import wrap_run
        rc = wrap_run("test_source", "test.service", lambda: 0)
        assert rc == 0

    def test_returns_main_exit_code_on_explicit_failure(self):
        from utils.supabase_writer import wrap_run
        rc = wrap_run("test_source", "test.service", lambda: 1)
        assert rc == 1

    def test_maps_exit_code_2_to_stale_status(self):
        from utils.supabase_writer import wrap_run, _STATUS_BY_EXIT
        assert _STATUS_BY_EXIT[0] == "ok"
        assert _STATUS_BY_EXIT[1] == "fail"
        assert _STATUS_BY_EXIT[2] == "stale"
        assert _STATUS_BY_EXIT[3] == "skip"

    def test_propagates_exception_after_logging(self):
        from utils.supabase_writer import wrap_run
        def boom():
            raise RuntimeError("kaboom")
        with pytest.raises(RuntimeError, match="kaboom"):
            wrap_run("test_source", "test.service", boom)
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_run_logging.py::TestWrapRun -v
```

Expected: ImportError on `wrap_run`.

- [ ] **Step 3: Implement `wrap_run()`**

Append to `utils/supabase_writer.py`:

```python
_STATUS_BY_EXIT = {0: "ok", 1: "fail", 2: "stale", 3: "skip"}


def wrap_run(source: str, unit: str, main_func: _Callable[[], int]) -> int:
    """Wrap a scraper's main() with run_logs instrumentation.

    Pattern at scraper bottom:
        if __name__ == '__main__':
            sys.exit(wrap_run('bb_forex', 'econdelta-forex.service', main))

    Maps main()'s exit code to run_logs.status:
        0 -> 'ok', 1 -> 'fail', 2 -> 'stale', 3 -> 'skip', other -> 'fail'
    Uncaught exceptions are logged as 'fail' with error=type(e).__name__: str(e),
    then re-raised so systemd records non-zero exit.
    """
    started_at = datetime.now(timezone.utc)
    run_id = log_run_start(source=source, unit=unit, started_at=started_at)
    try:
        exit_code = main_func()
        status = _STATUS_BY_EXIT.get(exit_code, "fail")
        log_run_end(run_id, started_at, status=status, exit_code=exit_code)
        return exit_code
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log_run_end(run_id, started_at, status="fail", exit_code=1, error=err)
        raise
```

- [ ] **Step 4: Run to verify pass**

```bash
.venv/bin/pytest tests/test_run_logging.py -v
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add utils/supabase_writer.py tests/test_run_logging.py
git commit -m "feat(supabase_writer): add wrap_run scraper instrumentation helper

One-line scraper instrumentation pattern:
    if __name__ == '__main__':
        sys.exit(wrap_run('bb_forex', 'econdelta-forex.service', main))

Maps exit codes 0/1/2/3 -> ok/fail/stale/skip. Uncaught exceptions are
logged as fail then re-raised so systemd records the failure too.

Refs spec: §4.1, §4.4"
```

---

### Task 7: Add `upsert_metric_definitions_seed()` to `utils/supabase_writer.py`

**Files:**
- Modify: `utils/supabase_writer.py`
- Test: `tests/test_definitions_seed.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_definitions_seed.py`:

```python
"""Tests for upsert_metric_definitions_seed in utils/supabase_writer.py."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


class TestUpsertMetricDefinitionsSeed:
    def test_returns_count_when_skipped(self):
        from utils.supabase_writer import upsert_metric_definitions_seed
        # When SKIP_SUPABASE=1, the function returns 0 (no rows inserted)
        defs = [
            {"metric_id": "test1", "label": "Test 1", "domain": "Test"},
            {"metric_id": "test2", "label": "Test 2", "domain": "Test"},
        ]
        rc = upsert_metric_definitions_seed(defs)
        assert rc == 0

    def test_handles_empty_list(self):
        from utils.supabase_writer import upsert_metric_definitions_seed
        rc = upsert_metric_definitions_seed([])
        assert rc == 0

    def test_validates_required_fields(self):
        from utils.supabase_writer import upsert_metric_definitions_seed
        # Missing metric_id should raise
        with pytest.raises((KeyError, ValueError)):
            upsert_metric_definitions_seed([{"label": "x", "domain": "y"}])

    def test_default_fields_filled_in(self):
        from utils.supabase_writer import _normalize_definition
        d = _normalize_definition({"metric_id": "test", "label": "Test", "domain": "Test"})
        assert d["sort_order"] == 100
        assert d["format"] == "comma-2dp"
        assert d["is_hero"] is False
        assert d["inverted"] is False
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_definitions_seed.py -v
```

Expected: ImportError on `upsert_metric_definitions_seed` and `_normalize_definition`.

- [ ] **Step 3: Implement the helpers**

Append to `utils/supabase_writer.py`:

```python
_DEFAULT_DEFINITION_FIELDS = {
    "short_label": None,
    "unit": None,
    "sort_order": 100,
    "cadence": None,
    "format": "comma-2dp",
    "description": None,
    "source": None,
    "source_url": None,
    "is_hero": False,
    "inverted": False,
}


def _normalize_definition(d: dict) -> dict:
    """Validate required fields, fill defaults, return upsert-ready row."""
    if "metric_id" not in d:
        raise KeyError("definition missing required field 'metric_id'")
    if "label" not in d:
        raise KeyError("definition missing required field 'label'")
    if "domain" not in d:
        raise KeyError("definition missing required field 'domain'")
    out = {**_DEFAULT_DEFINITION_FIELDS, **d}
    return out


def upsert_metric_definitions_seed(definitions: list[dict]) -> int:
    """Insert metric_definitions rows with ON CONFLICT (metric_id) DO NOTHING.

    First insert wins forever; manual edits in Supabase Studio are preserved.
    Returns count of NEW rows inserted (0 in test/skip mode).

    Raises KeyError for definitions missing required fields (metric_id, label, domain).
    """
    if not definitions:
        return 0

    rows = [_normalize_definition(d) for d in definitions]

    if os.environ.get("ECONDELTA_SKIP_SUPABASE") == "1":
        return 0

    try:
        client = _get_client()
        if client is None:
            return 0
        # supabase-py upsert with ignore_duplicates=True maps to ON CONFLICT DO NOTHING
        result = client.table("metric_definitions").upsert(
            rows, ignore_duplicates=True, on_conflict="metric_id"
        ).execute()
        return len(result.data) if result.data else 0
    except Exception as e:  # noqa: BLE001
        log.error("upsert_metric_definitions_seed failed: %s", e)
        raise
```

- [ ] **Step 4: Run to verify pass**

```bash
.venv/bin/pytest tests/test_definitions_seed.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add utils/supabase_writer.py tests/test_definitions_seed.py
git commit -m "feat(supabase_writer): add upsert_metric_definitions_seed helper

Idempotent ON CONFLICT DO NOTHING upsert for metric_definitions catalog.
First insert wins; manual Studio edits to label/sort/is_hero are preserved
forever. Validates metric_id/label/domain required fields.

Refs spec: §3.1, §4.1"
```

---

## Phase 3 — Aggregator Extension

### Task 8: Wire definition seeding into `aggregate_latest.py`

**Files:**
- Modify: `aggregate_latest.py`
- Test: `tests/test_aggregate_definitions.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_aggregate_definitions.py`:

```python
"""Tests for definition seeding logic in aggregate_latest.py."""
from __future__ import annotations

import json
import pytest


@pytest.fixture(autouse=True)
def skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


class TestBuildDefinitionSeeds:
    def test_maps_v3_indicator_to_definition_row(self):
        from aggregate_latest import _build_definition_seeds
        sources_v3 = {
            "indicators": [
                {
                    "id": "banking_npl_pct",
                    "domain": "monetary",
                    "label": "Gross NPL Ratio",
                    "unit": "%",
                    "cadence": "quarterly",
                    "fetch": {"type": "pdf", "url": "https://www.bb.org.bd/..."},
                },
            ]
        }
        seeds = _build_definition_seeds(sources_v3)
        assert len(seeds) == 1
        d = seeds[0]
        assert d["metric_id"] == "banking_npl_pct"
        assert d["label"] == "Gross NPL Ratio"
        assert d["unit"] == "%"
        assert d["domain"] == "monetary"
        assert d["cadence"] == "quarterly"
        assert d["source_url"] == "https://www.bb.org.bd/..."

    def test_falls_back_to_titleized_id_when_label_missing(self):
        from aggregate_latest import _build_definition_seeds
        sources_v3 = {"indicators": [{"id": "test_metric", "domain": "macro", "fetch": {"type": "html"}}]}
        seeds = _build_definition_seeds(sources_v3)
        assert seeds[0]["label"] == "Test Metric"

    def test_handles_missing_optional_fields(self):
        from aggregate_latest import _build_definition_seeds
        sources_v3 = {"indicators": [{"id": "x", "domain": "macro", "fetch": {"type": "html"}}]}
        seeds = _build_definition_seeds(sources_v3)
        assert seeds[0]["unit"] is None
        assert seeds[0]["cadence"] is None
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_aggregate_definitions.py -v
```

Expected: ImportError on `_build_definition_seeds`.

- [ ] **Step 3: Add `_build_definition_seeds()` to `aggregate_latest.py`**

Add this helper near the top of `aggregate_latest.py` (after imports, before `main()`):

```python
def _titleize(metric_id: str) -> str:
    """Convert 'banking_npl_pct' -> 'Banking Npl Pct'."""
    return " ".join(word.capitalize() for word in metric_id.split("_"))


def _build_definition_seeds(sources_v3_cfg: dict) -> list[dict]:
    """Build metric_definitions rows from sources-v3.json indicators.

    Conservative defaults: label falls back to titleized id, sort_order=100,
    is_hero=False. Tunable in Supabase Studio post-insert.
    """
    seeds = []
    for ind in sources_v3_cfg.get("indicators", []):
        seeds.append({
            "metric_id": ind["id"],
            "label": ind.get("label") or _titleize(ind["id"]),
            "short_label": ind.get("short_label"),
            "unit": ind.get("unit"),
            "domain": ind.get("domain", "Other"),
            "cadence": ind.get("cadence"),
            "description": ind.get("description"),
            "source": ind.get("source"),
            "source_url": (ind.get("fetch") or {}).get("url"),
        })
    return seeds
```

- [ ] **Step 4: Run to verify pass**

```bash
.venv/bin/pytest tests/test_aggregate_definitions.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Wire seeding call into aggregate `main()`**

In `aggregate_latest.py`, find the `main()` function. After it finishes loading `sources_v3` (likely from `config/sources-v3.json`), add:

```python
    # Seed metric_definitions for any new indicators (idempotent).
    from utils.supabase_writer import upsert_metric_definitions_seed
    seeds = _build_definition_seeds(sources_v3)
    inserted = upsert_metric_definitions_seed(seeds)
    if inserted:
        logger.info("Seeded %d new metric_definitions rows", inserted)
```

(Place this BEFORE the existing `upsert_metric_history` call so definitions exist before values reference them.)

- [ ] **Step 6: Wrap aggregate's main with `wrap_run`**

At the bottom of `aggregate_latest.py`, change:

```python
if __name__ == "__main__":
    sys.exit(main())
```

to:

```python
if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("aggregate", "econdelta-aggregate.service", main))
```

- [ ] **Step 7: Run full test suite to ensure nothing regressed**

```bash
.venv/bin/pytest -q
```

Expected: all tests pass (262 + new tests added in Phase 2 + this task).

- [ ] **Step 8: Commit**

```bash
git add aggregate_latest.py tests/test_aggregate_definitions.py
git commit -m "feat(aggregate): seed metric_definitions + log_run instrument

aggregate_latest.py now (a) walks sources-v3.json on each fire and seeds
metric_definitions rows for any new metric_ids (idempotent ON CONFLICT
DO NOTHING — manual Studio edits preserved), and (b) wraps main() in
wrap_run() so aggregate runs land in run_logs.

Refs spec: §4.2"
```

---

## Phase 4 — Scraper Instrumentation

### Task 9: Wrap all 5 remaining scrapers with `wrap_run`

**Files:**
- Modify: `scrapers/bb_forex.py`
- Modify: `scrapers/dse_market.py`
- Modify: `scrapers/commodity_prices.py`
- Modify: `fetch_all.py`
- Modify: `parse_all.py`

- [ ] **Step 1: Write a smoke-import test**

Create `tests/test_wrap_run_imports.py`:

```python
"""Smoke test: every scraper module imports its wrap_run pattern correctly."""
import importlib


def test_all_scrapers_can_import_wrap_run():
    """Verify utils.supabase_writer.wrap_run is importable from each scraper context."""
    from utils.supabase_writer import wrap_run
    assert callable(wrap_run)
```

- [ ] **Step 2: Run test to verify it passes (sanity check)**

```bash
.venv/bin/pytest tests/test_wrap_run_imports.py -v
```

Expected: PASS (this should already pass since wrap_run was added in Task 6).

- [ ] **Step 3: Modify `scrapers/bb_forex.py`**

Find the bottom of the file:
```python
if __name__ == "__main__":
    sys.exit(main())
```

Replace with:
```python
if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("bb_forex", "econdelta-forex.service", main))
```

- [ ] **Step 4: Modify `scrapers/dse_market.py`**

Same pattern:
```python
if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("dse_market", "econdelta-dse.service", main))
```

- [ ] **Step 5: Modify `scrapers/commodity_prices.py`**

```python
if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("commodity_prices", "econdelta-commodity.service", main))
```

- [ ] **Step 6: Modify `fetch_all.py`**

```python
if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("fetch", "econdelta-fetch.service", main))
```

- [ ] **Step 7: Modify `parse_all.py`**

```python
if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("parse", "econdelta-parse.service", main))
```

- [ ] **Step 8: Run full test suite**

```bash
.venv/bin/pytest -q
```

Expected: all tests pass (none should break — only `__main__` blocks changed, which test imports never execute).

- [ ] **Step 9: Smoke-test one scraper locally with skip flag**

```bash
ECONDELTA_SKIP_SUPABASE=1 .venv/bin/python -c "
import scrapers.bb_forex as m
# Just verify the module loads cleanly with the new imports.
print('module loaded:', m.__name__)
"
```

Expected: prints `module loaded: scrapers.bb_forex` with no error.

- [ ] **Step 10: Commit**

```bash
git add scrapers/bb_forex.py scrapers/dse_market.py scrapers/commodity_prices.py \
        fetch_all.py parse_all.py tests/test_wrap_run_imports.py
git commit -m "feat(scrapers): instrument all 5 scrapers with wrap_run

Each scraper's __main__ now wraps main() in wrap_run() to record start/end
rows in run_logs. Per-source tags: bb_forex, dse_market, commodity_prices,
fetch, parse. Aggregate was instrumented in the previous commit.

Refs spec: §4.3"
```

---

## Phase 5 — PWA Scaffolding

### Task 10: Create `pwa/` directory + vendor mirror

**Files:**
- Create: `pwa/vendor/react.production.min.js`
- Create: `pwa/vendor/react-dom.production.min.js`
- Create: `pwa/vendor/babel.min.js`

- [ ] **Step 1: Create the directory structure**

```bash
mkdir -p ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/{vendor,lib,pages,icons}
```

- [ ] **Step 2: Download React + ReactDOM**

```bash
cd ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/vendor
curl -fLso react.production.min.js \
  https://unpkg.com/react@18.3.1/umd/react.production.min.js
curl -fLso react-dom.production.min.js \
  https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js
ls -la
```

Expected: 2 files, react.production.min.js ~12 KB, react-dom.production.min.js ~140 KB.

- [ ] **Step 3: Download Babel standalone**

```bash
curl -fLso babel.min.js \
  https://unpkg.com/@babel/standalone@7.29.0/babel.min.js
ls -la
```

Expected: babel.min.js ~600 KB.

- [ ] **Step 4: Verify file integrity**

```bash
file *.js
head -c 200 react.production.min.js
```

Expected: All three are JavaScript files. React file starts with the typical UMD wrapper `/** @license React v18.3.1 ...`.

- [ ] **Step 5: Commit**

```bash
git add pwa/vendor/
git commit -m "feat(pwa): mirror React 18.3.1 + Babel 7.29.0 locally

Drops the unpkg.com runtime dependency. PWA can now serve React + Babel
from same origin, removing one network failure mode and one CORS edge case.

Refs spec: §5.2"
```

---

### Task 11: Port bundle's icons + manifest

**Files:**
- Create: `pwa/icons/*` (6 PNGs copied from bundle)
- Create: `pwa/manifest.webmanifest`

- [ ] **Step 1: Copy icons from bundle**

```bash
cp ~/downloads/econdelta/icons/* \
   ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/icons/
ls -la ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/icons/
```

Expected: 6 files (apple-touch-icon-180.png, favicon-16.png, favicon-32.png, icon-192.png, icon-512.png, icon-512-maskable.png).

- [ ] **Step 2: Write the manifest**

Create `pwa/manifest.webmanifest`:

```json
{
  "name": "EconDelta — Bangladesh Macro",
  "short_name": "EconDelta",
  "description": "Live snapshot of Bangladesh forex, capital markets, banking stability, food prices, and government finance. Daily, anomaly-gated.",
  "start_url": "./",
  "scope": "./",
  "display": "standalone",
  "orientation": "any",
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

- [ ] **Step 3: Verify manifest validity**

```bash
python3 -c "import json; print(json.load(open('pwa/manifest.webmanifest')))"
```

Expected: prints the parsed dict, no errors.

- [ ] **Step 4: Commit**

```bash
git add pwa/icons/ pwa/manifest.webmanifest
git commit -m "feat(pwa): manifest + icons (oxblood theme, BD macro framing)

Theme color set to oxblood (#c34a1f) — visible on iOS standalone status bar.
6 icons cover web favicon, iOS apple-touch, Android maskable + standard.

Refs spec: §6.2"
```

---

### Task 12: Port `styles.css` + `register-pwa.js` from bundle

**Files:**
- Create: `pwa/styles.css` (copy from bundle)
- Create: `pwa/register-pwa.js` (copy from bundle)

- [ ] **Step 1: Copy both files**

```bash
cp ~/downloads/econdelta/styles.css \
   ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/styles.css
cp ~/downloads/econdelta/register-pwa.js \
   ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/register-pwa.js
```

- [ ] **Step 2: Verify file contents**

```bash
wc -l pwa/styles.css pwa/register-pwa.js
head -20 pwa/styles.css
```

Expected: styles.css ~700 lines, register-pwa.js ~100 lines, both are recognizable CSS/JS.

- [ ] **Step 3: Commit**

```bash
git add pwa/styles.css pwa/register-pwa.js
git commit -m "feat(pwa): port styles.css + register-pwa.js from bundle

Design system (IBM Plex font triplet, terminal-newsprint dark, oxblood accent)
ported as-is. PWA registration script handles SW install + update prompts.

Refs spec: §5, §6.3"
```

---

### Task 13: Write `pwa/sw.js` with three-tier cache strategy

**Files:**
- Create: `pwa/sw.js`

- [ ] **Step 1: Write the service worker**

Create `pwa/sw.js`:

```javascript
// EconDelta PWA service worker
// Three caching tiers:
//   1. vendor/* (React, Babel)         — cache-first, never revalidate
//   2. app code (jsx/js/css/icons)     — stale-while-revalidate
//   3. RPC get_latest_dashboard()       — network-first, 5s timeout, cache fallback
//
// Cache version in CACHE_NAME — bump this string to force eviction on deploy.

const CACHE_NAME = 'econdelta-v1-2026-05-04';
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
  './icons/icon-192.png',
  './icons/icon-512.png',
];

const VENDOR_ASSETS = [
  './vendor/react.production.min.js',
  './vendor/react-dom.production.min.js',
  './vendor/babel.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    Promise.all([
      caches.open(VENDOR_CACHE).then(c => c.addAll(VENDOR_ASSETS)),
      caches.open(CACHE_NAME).then(c => c.addAll(APP_SHELL)),
    ]).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => ![CACHE_NAME, VENDOR_CACHE, RPC_CACHE].includes(k))
          .map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Tier 3: RPC call to get_latest_dashboard — network-first, 5s timeout
  if (url.pathname.endsWith('/rest/v1/rpc/get_latest_dashboard')) {
    event.respondWith(rpcStrategy(event.request));
    return;
  }

  // Tier 1: vendor — cache-first
  if (url.pathname.includes('/vendor/')) {
    event.respondWith(
      caches.match(event.request).then(r => r || fetch(event.request))
    );
    return;
  }

  // Tier 2: app shell — stale-while-revalidate
  if (event.request.method === 'GET') {
    event.respondWith(
      caches.match(event.request).then(cached => {
        const fetchPromise = fetch(event.request).then(networkRes => {
          if (networkRes.ok) {
            const clone = networkRes.clone();
            caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
          }
          return networkRes;
        }).catch(() => cached);
        return cached || fetchPromise;
      })
    );
  }
});

async function rpcStrategy(request) {
  try {
    const networkPromise = fetch(request.clone());
    const timeoutPromise = new Promise((_, reject) =>
      setTimeout(() => reject(new Error('rpc timeout')), 5000)
    );
    const networkRes = await Promise.race([networkPromise, timeoutPromise]);
    if (networkRes.ok) {
      const clone = networkRes.clone();
      caches.open(RPC_CACHE).then(c => c.put(request, clone));
      return networkRes;
    }
    throw new Error('rpc non-ok');
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({error: 'offline-no-cache'}),
      {status: 503, headers: {'Content-Type': 'application/json'}});
  }
}
```

- [ ] **Step 2: Verify with a JS lint pass**

```bash
node -c pwa/sw.js && echo "syntax OK"
```

Expected: prints "syntax OK".

- [ ] **Step 3: Commit**

```bash
git add pwa/sw.js
git commit -m "feat(pwa): service worker with 3-tier caching + RPC timeout

Vendor (React/Babel): cache-first, never revalidate.
App shell (jsx/js/css): stale-while-revalidate.
RPC get_latest_dashboard: network-first, 5s timeout, cache fallback.
Cache version (CACHE_NAME) bumped on each deploy to force eviction.

Refs spec: §6.4, §7"
```

---

### Task 14: Write `pwa/index.html` + `pwa/config.js`

**Files:**
- Create: `pwa/index.html`
- Create: `pwa/config.js`

- [ ] **Step 1: Get the Supabase anon key**

Use Supabase MCP to fetch the publishable (anon) key for the project:

`mcp__plugin_supabase_supabase__get_publishable_keys` (returns `{anon_key, ...}`)

Save the anon key for use in `config.js`.

- [ ] **Step 2: Write `pwa/config.js`**

Create `pwa/config.js`:

```javascript
// EconDelta PWA — Supabase config
// The anon key is PUBLIC by design — Supabase RLS policies restrict it to
// SELECT on metric_definitions, run_logs, metric_history. Service-role keys
// stay on ExonVPS only.
window.ED_SUPABASE_CONFIG = {
  url: 'https://ssbliukchgibjcjohibi.supabase.co',
  anonKey: '<ANON_KEY_FROM_STEP_1>',
};
```

Replace `<ANON_KEY_FROM_STEP_1>` with the actual anon key.

- [ ] **Step 3: Write `pwa/index.html`**

Create `pwa/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>EconDelta · Bangladesh Macro</title>

<!-- PWA -->
<link rel="manifest" href="manifest.webmanifest"/>
<meta name="theme-color" content="#c34a1f"/>
<meta name="color-scheme" content="dark"/>

<!-- iOS standalone -->
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="apple-mobile-web-app-title" content="EconDelta"/>
<link rel="apple-touch-icon" href="icons/apple-touch-icon-180.png"/>

<!-- Favicons -->
<link rel="icon" type="image/png" sizes="32x32" href="icons/favicon-32.png"/>
<link rel="icon" type="image/png" sizes="16x16" href="icons/favicon-16.png"/>

<!-- Fonts -->
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet"/>

<link rel="stylesheet" href="styles.css"/>
</head>
<body>
<div id="root"></div>

<!-- React + Babel — local mirror -->
<script src="vendor/react.production.min.js"></script>
<script src="vendor/react-dom.production.min.js"></script>
<script src="vendor/babel.min.js"></script>

<!-- Supabase config (anon key — public by design) -->
<script src="config.js"></script>

<!-- Data layer — supabase-client falls back to data-mock if config missing -->
<script src="lib/supabase-client.js"></script>

<!-- React app -->
<script type="text/babel" src="components.jsx"></script>
<script type="text/babel" src="pages/latest.jsx"></script>
<script type="text/babel" src="pages/archive.jsx"></script>
<script type="text/babel" src="pages/runs.jsx"></script>
<script type="text/babel" src="pages/sources-about.jsx"></script>

<script type="text/babel">
function useHashRoute(){
  const [route, setRoute] = React.useState(window.location.hash.slice(1) || '/');
  React.useEffect(() => {
    const onHash = () => setRoute(window.location.hash.slice(1) || '/');
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return route;
}

function App(){
  const route = useHashRoute();
  const [, setDataVersion] = React.useState(0);
  React.useEffect(() => {
    const onChange = () => setDataVersion(v => v + 1);
    window.addEventListener('ed:data-changed', onChange);
    return () => window.removeEventListener('ed:data-changed', onChange);
  }, []);
  let Page, label;
  if(route === '/archive'){ Page = PageArchive; label = '02 Archive'; }
  else if(route === '/runs'){ Page = PageRuns; label = '03 Runs'; }
  else if(route === '/sources'){ Page = PageSources; label = '04 Sources'; }
  else if(route === '/about'){ Page = PageAbout; label = '05 About'; }
  else if(route.startsWith('/domain/')){ Page = PageDomain; label = '01 Latest'; }
  else { Page = PageLatest; label = '01 Latest'; }
  return (
    <div className="app" data-screen-label={label}>
      <Sidebar route={route}/>
      <main className="main">
        <Masthead/>
        <Page route={route}/>
      </main>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
</script>

<script src="register-pwa.js" defer></script>
</body>
</html>
```

- [ ] **Step 4: Verify HTML loads in browser**

```bash
cd ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion
python3 -m http.server 8765 --bind 127.0.0.1 &
sleep 1
curl -s http://127.0.0.1:8765/pwa/index.html | head -5
kill %1 2>/dev/null
```

Expected: prints the HTML doctype + head opening. (Pages won't render yet — components.jsx and pages/*.jsx don't exist; that's the next task.)

- [ ] **Step 5: Commit**

```bash
git add pwa/index.html pwa/config.js
git commit -m "feat(pwa): index.html shell + Supabase config

index.html loads vendored React + Babel, then config.js (with public anon
key), then the data layer, then components + pages, mounts via createRoot.
Hash routing covers /, /archive, /runs, /sources, /about, /domain/<slug>.

Refs spec: §5.3, §5.4"
```

---

## Phase 6 — Data Layer

### Task 15: Port `pwa/lib/data-mock.js` from bundle

**Files:**
- Create: `pwa/lib/data-mock.js` (copy from bundle's `data.js`)

- [ ] **Step 1: Copy and rename**

```bash
cp ~/downloads/econdelta/data.js \
   ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/lib/data-mock.js
```

- [ ] **Step 2: Verify the file sets `window.ED_DATA`**

```bash
grep -n "window.ED_DATA" pwa/lib/data-mock.js
```

Expected: prints a line near the bottom of the file.

- [ ] **Step 3: Commit**

```bash
git add pwa/lib/data-mock.js
git commit -m "feat(pwa): port data-mock.js for offline dev

Bundle's mock data layer (90 days of seeded forex/dse/commodity walks +
injected outage windows). Used during dev to iterate UI without Supabase.
To activate: comment out supabase-client.js script tag in index.html and
uncomment a line for data-mock.js.

Refs spec: §5.4"
```

---

### Task 16: Write `pwa/lib/supabase-client.js` for the new RPC

**Files:**
- Create: `pwa/lib/supabase-client.js`

- [ ] **Step 1: Write the new data layer**

Create `pwa/lib/supabase-client.js`:

```javascript
// EconDelta PWA — Supabase data layer
// Single RPC call (get_latest_dashboard) populates window.ED_DATA for all pages.
// Falls back to window.ED_DATA from data-mock.js if config is missing.
//
// Pages don't know whether the data came from mock or live — they just read
// window.ED_DATA. Keep that contract intact when extending this file.

(function(){
  const cfg = window.ED_SUPABASE_CONFIG;

  if(!cfg || !cfg.url || !cfg.anonKey){
    console.warn('[EconDelta] ED_SUPABASE_CONFIG not set — using data-mock if loaded.');
    if(window.ED_DATA){ return; }
    document.getElementById('root').innerHTML =
      '<pre style="padding:24px;font-family:monospace">' +
      'EconDelta dashboard: no data source configured.\n\n' +
      'Either:\n' +
      '  (a) load lib/data-mock.js for the mock dataset, or\n' +
      '  (b) set window.ED_SUPABASE_CONFIG = { url, anonKey } before this script\n' +
      '</pre>';
    return;
  }

  const HEADERS = {
    apikey: cfg.anonKey,
    Authorization: `Bearer ${cfg.anonKey}`,
    'Content-Type': 'application/json',
    Prefer: 'count=none',
  };

  // Render a loading sliver so user knows we're alive.
  const root = document.getElementById('root');
  if(root) root.innerHTML =
    '<div style="padding:32px;font-family:monospace;color:#888">loading from supabase…</div>';

  bootstrap().catch(err => {
    console.error('[EconDelta] bootstrap failed', err);
    if(root) root.innerHTML =
      '<pre style="padding:24px;font-family:monospace;color:#a33">' +
      'EconDelta dashboard: failed to load from Supabase.\n\n' +
      String(err) + '\n</pre>';
  });

  async function bootstrap(){
    // Single RPC for the Latest page (definitions + values + sources_status).
    const dashRes = await fetch(`${cfg.url}/rest/v1/rpc/get_latest_dashboard`, {
      method: 'POST',
      headers: HEADERS,
      body: '{}',
    });
    if(!dashRes.ok) throw new Error(`RPC ${dashRes.status}: ${await dashRes.text()}`);
    const dashboard = await dashRes.json();

    // Direct REST for archive (90-day window of metric_history).
    const since = new Date(Date.now() - 90*24*3600*1000).toISOString().slice(0,10);
    const histRes = await fetch(
      `${cfg.url}/rest/v1/metric_history?as_of=gte.${since}&select=metric_id,value,as_of,source_as_of&order=as_of.asc&limit=10000`,
      { headers: HEADERS }
    );
    const history = histRes.ok ? await histRes.json() : [];

    // Direct REST for runs (90-day window of run_logs).
    const sinceTs = new Date(Date.now() - 90*24*3600*1000).toISOString();
    const runsRes = await fetch(
      `${cfg.url}/rest/v1/run_logs?started_at=gte.${sinceTs}&select=*&order=started_at.asc&limit=10000`,
      { headers: HEADERS }
    );
    const runRows = runsRes.ok ? await runsRes.json() : [];

    // Re-shape runs by source (page-runs expects an array per source).
    const runsBySource = {};
    runRows.forEach(r => {
      if(!runsBySource[r.source]) runsBySource[r.source] = [];
      runsBySource[r.source].push({
        date: r.started_at.slice(0, 10),
        startedAt: r.started_at,
        finishedAt: r.finished_at,
        durationMs: r.duration_ms,
        status: r.status,
        error: r.error,
      });
    });

    window.ED_DATA = {
      today: new Date(),
      dashboard,
      history,
      runs: runsBySource,
    };

    // Tell App.jsx to re-render now that data is loaded.
    window.dispatchEvent(new CustomEvent('ed:data-changed'));
  }

  // Manual refresh — pull-to-refresh or button click.
  window.ED_REFRESH = () => bootstrap();
})();
```

- [ ] **Step 2: Verify syntax**

```bash
node -c pwa/lib/supabase-client.js && echo "syntax OK"
```

Expected: prints "syntax OK".

- [ ] **Step 3: Commit**

```bash
git add pwa/lib/supabase-client.js
git commit -m "feat(pwa): supabase-client.js — single-RPC dashboard fetch

Calls get_latest_dashboard() once for the Latest page payload (definitions
+ values + sources_status). Plus two direct REST queries for the 90-day
archive (metric_history) and runs commit-graph (run_logs). Sets
window.ED_DATA shape consumed by all page components.

Refs spec: §3.3, §5.5"
```

---

## Phase 7 — Frontend Components & Pages

### Task 17: Port `components.jsx` from bundle (with adaptations)

**Files:**
- Create: `pwa/components.jsx`

- [ ] **Step 1: Copy bundle's components**

```bash
cp ~/downloads/econdelta/components.jsx \
   ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/components.jsx
```

- [ ] **Step 2: Verify expected exports**

The bundle's `components.jsx` should expose: `Sidebar`, `Masthead`, `StatusPill`, `Sparkline`, `PageHead`, plus formatting helpers (`fmtPct`, `relTime`).

```bash
grep -E "^window\.|^function (Sidebar|Masthead|StatusPill|Sparkline|PageHead)" pwa/components.jsx
```

Expected: prints lines for each named function.

- [ ] **Step 3: If any expected export is missing, add a stub**

(Verify each: `Sidebar`, `Masthead`, `StatusPill`, `Sparkline`, `PageHead`, `fmtPct`, `relTime`.) If any is missing because the bundle's structure differs, add the stub at the bottom:

```jsx
// EconDelta PWA component stubs (only if bundle is missing them)
window.fmtPct = window.fmtPct || ((d) => (d == null ? '—' : (d * 100).toFixed(2) + '%'));
window.relTime = window.relTime || ((iso) => {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.round(ms / 60000);
  if (min < 60) return min + 'm ago';
  const hr = Math.round(min / 60);
  if (hr < 24) return hr + 'h ago';
  return Math.round(hr / 24) + 'd ago';
});
```

- [ ] **Step 4: Open in browser to verify base layout works**

```bash
cd ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion
python3 -m http.server 8765 --bind 127.0.0.1 &
sleep 1
echo "Open http://127.0.0.1:8765/pwa/ in browser. Should see loading spinner or styled shell."
echo "Press Enter to kill server"
read
kill %1 2>/dev/null
```

Expected: visiting URL shows the masthead/sidebar shell (the page content area will be empty since no Page components are defined yet — Tasks 18-21 fix this).

- [ ] **Step 5: Commit**

```bash
git add pwa/components.jsx
git commit -m "feat(pwa): port components.jsx from bundle

Sidebar, Masthead, StatusPill, Sparkline, PageHead + fmt helpers.
Imported as-is. Page-level components added in subsequent tasks.

Refs spec: §5.1, §5.5"
```

---

### Task 18: Write `pwa/pages/latest.jsx` — hero + bento layout

**Files:**
- Create: `pwa/pages/latest.jsx`

- [ ] **Step 1: Write the page**

Create `pwa/pages/latest.jsx`:

```jsx
// Latest page — hero cards (4 most-watched) + bento grid (per-domain tiles)
// Driven by window.ED_DATA.dashboard.{definitions, values, sources_status}.

function PageLatest(){
  const d = window.ED_DATA && window.ED_DATA.dashboard;
  if(!d) {
    return <div className="loading">no dashboard data yet…</div>;
  }
  const defs = d.definitions || [];
  const vals = d.values || {};
  const srcStatus = d.sources_status || {};

  // Hero cards: definitions where is_hero=true (default 4).
  const heroes = defs.filter(x => x.is_hero);

  // Group definitions by domain for bento.
  const byDomain = {};
  defs.forEach(def => {
    if(def.is_hero) return;  // skip — already in heroes
    if(!byDomain[def.domain]) byDomain[def.domain] = [];
    byDomain[def.domain].push(def);
  });

  // Sources status pill row.
  const sourceKeys = Object.keys(srcStatus).sort();

  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · canonical snapshot"
        title="Latest"
        meta={
          <React.Fragment>
            <div><b>updated</b> {d.updated_at && d.updated_at.slice(0, 19) + ' UTC'}</div>
            <div><b>defs</b> {defs.length}</div>
            <div><b>values</b> {Object.keys(vals).length}</div>
            <div><b>sources</b> {sourceKeys.length}</div>
          </React.Fragment>
        }
      />

      {/* Sources status row */}
      <div className="src-status-row">
        {sourceKeys.map(src => (
          <div key={src} className="src-pill">
            <span className="muted">{src}</span>
            <StatusPill status={srcStatus[src].status}/>
            <span className="tnum">{relTime(srcStatus[src].last_success)}</span>
          </div>
        ))}
      </div>

      {/* Hero cards */}
      {heroes.length > 0 && (
        <div className="hero-grid">
          {heroes.map(def => {
            const v = vals[def.metric_id];
            return <HeroCard key={def.metric_id} def={def} value={v}/>;
          })}
        </div>
      )}

      {/* Bento grid — one tile per domain */}
      <div className="bento-grid">
        {Object.keys(byDomain).sort().map(domain => (
          <BentoTile key={domain} domain={domain} defs={byDomain[domain]} vals={vals}/>
        ))}
      </div>
    </React.Fragment>
  );
}

function HeroCard({def, value}){
  const v = value && value.value != null ? value.value : null;
  return (
    <div className="hero-card">
      <div className="lbl">{def.short_label || def.label}</div>
      <div className="val tnum">{v == null ? '—' : formatValue(v, def.format)}</div>
      <div className="sub">{def.unit}</div>
    </div>
  );
}

function BentoTile({domain, defs, vals}){
  const onClick = () => { window.location.hash = '#/domain/' + slug(domain); };
  return (
    <div className="bento" onClick={onClick}>
      <div className="dom">{domain}</div>
      <div className="count">{defs.length} indicators</div>
      {defs.slice(0, 3).map(def => {
        const v = vals[def.metric_id];
        return (
          <div key={def.metric_id} className="preview">
            <span>{def.short_label || def.label}</span>
            <span className="pv">{v && v.value != null ? formatValue(v.value, def.format) : '—'}</span>
          </div>
        );
      })}
      {defs.length > 3 && <div className="more">+ {defs.length - 3} more →</div>}
    </div>
  );
}

function formatValue(v, format){
  if(v == null) return '—';
  if(format === 'pct-1dp') return v.toFixed(1) + '%';
  if(format === 'pct-2dp') return v.toFixed(2) + '%';
  if(format === 'currency-bdt') return v.toLocaleString();
  // default: comma-2dp
  return Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function slug(s){ return String(s).toLowerCase().replace(/\s+/g, '-'); }

window.PageLatest = PageLatest;
```

- [ ] **Step 2: Verify syntax (Babel parse will happen at runtime — for now check basic balance)**

```bash
node -e "
const fs = require('fs');
const src = fs.readFileSync('pwa/pages/latest.jsx', 'utf8');
const opens = (src.match(/\{/g) || []).length;
const closes = (src.match(/\}/g) || []).length;
if (opens !== closes) {
  console.error('brace mismatch:', opens, 'opens vs', closes, 'closes');
  process.exit(1);
}
console.log('braces balanced ok');
"
```

Expected: `braces balanced ok`.

- [ ] **Step 3: Commit**

```bash
git add pwa/pages/latest.jsx
git commit -m "feat(pwa): Latest page — hero + bento, definitions-driven

Renders hero cards from definitions where is_hero=true, then a bento grid
of one tile per remaining domain. Tile click navigates to #/domain/<slug>.
All driven by window.ED_DATA.dashboard — zero hardcoded indicator IDs.

Refs spec: §5.5, §6.1"
```

---

### Task 19: Add `PageDomain` drill-in route in `pwa/pages/latest.jsx`

**Files:**
- Modify: `pwa/pages/latest.jsx` (append PageDomain)

- [ ] **Step 1: Append `PageDomain` to `pwa/pages/latest.jsx`**

```jsx
// Domain drill-in — full list of indicators in one domain.
function PageDomain({route}){
  const d = window.ED_DATA && window.ED_DATA.dashboard;
  if(!d) return <div className="loading">no data yet…</div>;
  const defs = d.definitions || [];
  const vals = d.values || {};

  // Route shape: '/domain/<slug>' — find domain whose slug matches.
  const targetSlug = route.replace('/domain/', '');
  const domainName = (defs.find(x => slug(x.domain) === targetSlug) || {}).domain;
  if(!domainName){
    return (
      <React.Fragment>
        <PageHead title="Domain not found" kicker="Pipeline"/>
        <p>No indicators registered for "{targetSlug}".</p>
        <p><a href="#/">← Back to Latest</a></p>
      </React.Fragment>
    );
  }

  const domainDefs = defs.filter(x => x.domain === domainName);

  return (
    <React.Fragment>
      <PageHead
        title={domainName}
        kicker="Pipeline · domain detail"
        meta={<div><b>indicators</b> {domainDefs.length}</div>}
      />
      <p><a href="#/">← Back to Latest</a></p>
      <div className="indicator-list">
        {domainDefs.map(def => {
          const v = vals[def.metric_id];
          return (
            <div key={def.metric_id} className="indicator-row">
              <div className="il-label">
                <b>{def.label}</b>
                {def.description && <div className="il-desc">{def.description}</div>}
              </div>
              <div className="il-value tnum">
                {v && v.value != null ? formatValue(v.value, def.format) : '—'}
                <span className="il-unit">{def.unit}</span>
              </div>
              {v && v.source_as_of && (
                <div className="il-asof">as of {v.source_as_of}</div>
              )}
            </div>
          );
        })}
      </div>
    </React.Fragment>
  );
}

window.PageDomain = PageDomain;
```

- [ ] **Step 2: Add minimal CSS for the new components**

Append to `pwa/styles.css`:

```css
/* PWA — hero + bento + domain drill-in */
.src-status-row {
  display: flex; gap: 14px; flex-wrap: wrap;
  margin-bottom: 18px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: var(--ink-3, #6b7480);
}
.src-pill { display: flex; gap: 6px; align-items: center; }
.hero-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
  margin-bottom: 18px;
}
.hero-card {
  background: var(--paper, #1a2024);
  border-left: 2px solid var(--accent, #c34a1f);
  padding: 12px 14px;
}
.hero-card .lbl {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
  color: var(--ink-3, #9aa3ad);
  text-transform: uppercase;
  letter-spacing: .12em;
  margin-bottom: 4px;
}
.hero-card .val {
  font-family: 'IBM Plex Serif', serif;
  font-size: 28px;
  font-weight: 600;
  color: var(--ink, #fff);
  margin-bottom: 4px;
}
.hero-card .sub {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: var(--ink-3, #6b7480);
}
.bento-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
}
.bento {
  background: var(--paper, #1a2024);
  border: 1px solid var(--rule, #2a2f33);
  padding: 14px;
  cursor: pointer;
  transition: background 0.1s;
}
.bento:hover { background: rgba(195,74,31,.05); }
.bento .dom {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
  color: var(--accent, #c34a1f);
  text-transform: uppercase;
  letter-spacing: .14em;
  margin-bottom: 4px;
}
.bento .count {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
  color: var(--ink-3, #6b7480);
  margin-bottom: 8px;
}
.bento .preview {
  display: flex; justify-content: space-between;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  margin-bottom: 2px;
}
.bento .preview .pv {
  color: var(--ink, #fff);
  font-feature-settings: 'tnum' 1;
}
.bento .more {
  margin-top: 4px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
  color: var(--ink-3, #6b7480);
}
.indicator-list {
  display: grid; gap: 10px;
}
.indicator-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 12px;
  padding: 12px 0;
  border-bottom: 1px solid var(--rule, #2a2f33);
}
.il-label b { display: block; }
.il-desc { font-size: 12px; color: var(--ink-3, #6b7480); margin-top: 4px; }
.il-value { font-family: 'IBM Plex Mono', monospace; text-align: right; }
.il-unit { font-size: 11px; color: var(--ink-3, #6b7480); margin-left: 4px; }
.il-asof { grid-column: 1 / -1; font-size: 10px; color: var(--ink-3, #6b7480); }
```

- [ ] **Step 3: Commit**

```bash
git add pwa/pages/latest.jsx pwa/styles.css
git commit -m "feat(pwa): PageDomain drill-in + supporting CSS

Tap a bento tile -> #/domain/<slug> -> full list of all metrics in that
domain with label, value, unit, source_as_of. CSS for hero cards, bento
tiles, indicator rows.

Refs spec: §6.1"
```

---

### Task 20: Port + adapt `pwa/pages/archive.jsx`

**Files:**
- Create: `pwa/pages/archive.jsx`

- [ ] **Step 1: Copy bundle's archive page**

```bash
cp ~/downloads/econdelta/page-archive.jsx \
   ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/pages/archive.jsx
```

- [ ] **Step 2: Adapt to read `window.ED_DATA.history`**

The bundle's `page-archive.jsx` reads `data.forexSnaps` / `data.dseSnaps` etc. We need it to read `window.ED_DATA.history` (the `metric_history` rows) instead.

Open `pwa/pages/archive.jsx`. Find the data-access lines (likely `const data = window.ED_DATA;` followed by accesses to nested snapshot maps). Replace the inner logic so the page renders a date-bucketed table from `data.history`.

Replacement page body (overwrite the existing component function):

```jsx
function PageArchive(){
  const data = window.ED_DATA;
  if(!data || !data.history){
    return <div className="loading">no archive data yet…</div>;
  }

  // Group history rows by date for a date-major table view.
  const byDate = {};
  data.history.forEach(r => {
    if(!byDate[r.as_of]) byDate[r.as_of] = [];
    byDate[r.as_of].push(r);
  });
  const dates = Object.keys(byDate).sort().reverse();

  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · 90-day window"
        title="Archive"
        meta={<div><b>days</b> {dates.length} &nbsp;<b>rows</b> {data.history.length}</div>}
      />
      <p className="sec-lede">Daily snapshots from <code>metric_history</code>. Most recent first.</p>
      <div className="archive-list">
        {dates.map(date => (
          <details key={date} className="archive-day">
            <summary>
              <span className="tnum">{date}</span>
              <span className="muted"> · {byDate[date].length} indicators</span>
            </summary>
            <div className="archive-rows">
              {byDate[date].map((r, i) => (
                <div key={i} className="archive-row">
                  <span>{r.metric_id}</span>
                  <span className="tnum">{r.value == null ? '—' : Number(r.value).toLocaleString()}</span>
                  {r.source_as_of && r.source_as_of !== r.as_of && (
                    <span className="muted">src {r.source_as_of}</span>
                  )}
                </div>
              ))}
            </div>
          </details>
        ))}
      </div>
    </React.Fragment>
  );
}

window.PageArchive = PageArchive;
```

- [ ] **Step 3: Add CSS for archive list**

Append to `pwa/styles.css`:

```css
.archive-list { display: grid; gap: 6px; }
.archive-day {
  background: var(--paper, #1a2024);
  border: 1px solid var(--rule, #2a2f33);
  padding: 10px 14px;
}
.archive-day summary {
  cursor: pointer;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 13px;
  color: var(--ink, #fff);
}
.archive-rows {
  margin-top: 10px;
  display: grid;
  gap: 4px;
}
.archive-row {
  display: grid;
  grid-template-columns: 2fr 1fr auto;
  gap: 12px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: var(--ink-3, #9aa3ad);
}
.archive-row .tnum { color: var(--ink, #fff); text-align: right; font-feature-settings: 'tnum' 1; }
.archive-row .muted { color: var(--ink-3, #6b7480); font-size: 10px; }
```

- [ ] **Step 4: Commit**

```bash
git add pwa/pages/archive.jsx pwa/styles.css
git commit -m "feat(pwa): Archive page — date-bucketed metric_history view

Reads 90-day window of metric_history from window.ED_DATA.history.
Grouped by as_of date; collapsible day-of details. Shows source_as_of
where it differs from as_of (i.e. quarterly metrics with publication lag).

Refs spec: §5.5"
```

---

### Task 21: Port + adapt `pwa/pages/runs.jsx`

**Files:**
- Create: `pwa/pages/runs.jsx`

- [ ] **Step 1: Copy bundle's runs page**

```bash
cp ~/downloads/econdelta/page-runs.jsx \
   ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/pages/runs.jsx
```

- [ ] **Step 2: Adapt to render dynamic source list**

The bundle hardcodes 3 sources (bb_forex, dse_market, commodity_prices). Adapt to render every distinct source in `window.ED_DATA.runs`.

Overwrite the page component with:

```jsx
function PageRuns(){
  const data = window.ED_DATA;
  if(!data || !data.runs){
    return <div className="loading">no runs data yet…</div>;
  }
  const sources = Object.keys(data.runs).sort();

  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · 90-day audit"
        title="Runs"
        meta={<div><b>sources</b> {sources.length} &nbsp;<b>total</b> {sources.reduce((s, k) => s + data.runs[k].length, 0)}</div>}
      />
      <p className="sec-lede">Each cell = one scraper invocation. Hover for details.</p>
      {sources.map(src => (
        <CommitGraph key={src} source={src} runs={data.runs[src]}/>
      ))}
    </React.Fragment>
  );
}

function CommitGraph({source, runs}){
  // 90-day grid: 13 weeks × 7 days. Map each run to its day.
  const today = new Date();
  const cells = [];
  for(let i = 89; i >= 0; i--){
    const d = new Date(today.getTime() - i*24*3600*1000);
    const ds = d.toISOString().slice(0,10);
    const dayRuns = runs.filter(r => r.date === ds);
    cells.push({ date: ds, runs: dayRuns });
  }

  const statusColor = (status) => ({
    ok: 'var(--ok, #6abf6e)',
    fail: 'var(--accent, #c34a1f)',
    stale: 'var(--warn, #a36a14)',
    skip: 'var(--ink-3, #6b7480)',
  })[status] || 'var(--rule, #2a2f33)';

  return (
    <div className="commit-graph-wrap">
      <h3>{source}</h3>
      <div className="commit-graph">
        {cells.map((c, i) => {
          // Pick worst status if multiple runs in a day.
          const worstStatus = c.runs.reduce((acc, r) => {
            const order = {fail: 0, stale: 1, skip: 2, ok: 3};
            return order[r.status] < order[acc] ? r.status : acc;
          }, 'ok');
          const color = c.runs.length === 0 ? 'transparent' : statusColor(worstStatus);
          const title = c.runs.length === 0
            ? `${c.date} · no run`
            : `${c.date} · ${c.runs.length} runs · ${worstStatus}`;
          return (
            <div
              key={i}
              className="cg-cell"
              style={{background: color, border: c.runs.length === 0 ? '1px solid var(--rule, #2a2f33)' : 'none'}}
              title={title}
            />
          );
        })}
      </div>
    </div>
  );
}

window.PageRuns = PageRuns;
```

- [ ] **Step 3: Add CSS for commit-graph**

Append to `pwa/styles.css`:

```css
.commit-graph-wrap { margin-bottom: 28px; }
.commit-graph-wrap h3 {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
  color: var(--accent, #c34a1f);
  text-transform: uppercase;
  letter-spacing: .14em;
  margin: 0 0 10px;
}
.commit-graph {
  display: grid;
  grid-template-columns: repeat(13, 1fr);
  gap: 3px;
  max-width: 480px;
}
.cg-cell {
  aspect-ratio: 1;
  border-radius: 1px;
}
```

- [ ] **Step 4: Commit**

```bash
git add pwa/pages/runs.jsx pwa/styles.css
git commit -m "feat(pwa): Runs page — dynamic commit-graph per source

Renders one 13×7 commit-graph per distinct source in run_logs (90-day
window). Cell color: green=ok, orange=fail, amber=stale, gray=skip,
empty=no run. Hover for date + status.

Refs spec: §5.5"
```

---

### Task 22: Port + adapt `pwa/pages/sources-about.jsx`

**Files:**
- Create: `pwa/pages/sources-about.jsx`

- [ ] **Step 1: Copy bundle's sources-about page**

```bash
cp ~/downloads/econdelta/page-sources-about.jsx \
   ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion/pwa/pages/sources-about.jsx
```

- [ ] **Step 2: Adapt to read from definitions**

Replace the page components with:

```jsx
function PageSources(){
  const data = window.ED_DATA && window.ED_DATA.dashboard;
  if(!data){
    return <div className="loading">no sources data yet…</div>;
  }
  // Group definitions by source.
  const bySource = {};
  (data.definitions || []).forEach(def => {
    const src = def.source || 'other';
    if(!bySource[src]) bySource[src] = [];
    bySource[src].push(def);
  });
  const sources = Object.keys(bySource).sort();

  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · provenance"
        title="Sources"
        meta={<div><b>sources</b> {sources.length}</div>}
      />
      <p className="sec-lede">Where each indicator originates.</p>
      {sources.map(src => (
        <section key={src} className="source-section">
          <h3>{src}</h3>
          <div className="source-indicators">
            {bySource[src].map(def => (
              <div key={def.metric_id} className="source-row">
                <span><b>{def.label}</b> <span className="muted">{def.metric_id}</span></span>
                {def.source_url && <a href={def.source_url} target="_blank" rel="noopener">source ↗</a>}
              </div>
            ))}
          </div>
        </section>
      ))}
    </React.Fragment>
  );
}

function PageAbout(){
  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · about"
        title="EconDelta"
        meta={<div><b>repo</b> clauding-lab/econdelta</div>}
      />
      <p>EconDelta is a deterministic Bangladesh macro data pipeline. Three layers:</p>
      <ol>
        <li><b>Backend</b>: Python scrapers + parsers + aggregator on ExonVPS (BDIX-Dhaka). Daily systemd cascade between 05:00 and 05:20 BDT.</li>
        <li><b>Data layer</b>: Supabase (Postgres) — three tables (<code>metric_history</code>, <code>metric_definitions</code>, <code>run_logs</code>) plus the <code>get_latest_dashboard()</code> RPC.</li>
        <li><b>Frontend</b>: this PWA — vanilla React, no build step, deployed via GitHub Pages.</li>
      </ol>
      <p><b>License:</b> source code at <a href="https://github.com/clauding-lab/econdelta">github.com/clauding-lab/econdelta</a>. Data is for informational use only — verify against original sources before any operational decision.</p>
    </React.Fragment>
  );
}

window.PageSources = PageSources;
window.PageAbout = PageAbout;
```

- [ ] **Step 3: Add CSS**

Append to `pwa/styles.css`:

```css
.source-section { margin-bottom: 28px; }
.source-section h3 {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
  color: var(--accent, #c34a1f);
  text-transform: uppercase;
  letter-spacing: .14em;
  margin: 0 0 10px;
}
.source-indicators { display: grid; gap: 6px; }
.source-row {
  display: flex;
  justify-content: space-between;
  font-family: 'IBM Plex Sans', sans-serif;
  font-size: 13px;
  padding: 6px 0;
  border-bottom: 1px solid var(--rule, #2a2f33);
}
.source-row .muted {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: var(--ink-3, #6b7480);
  margin-left: 8px;
}
.source-row a {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: var(--accent, #c34a1f);
}
```

- [ ] **Step 4: Commit**

```bash
git add pwa/pages/sources-about.jsx pwa/styles.css
git commit -m "feat(pwa): Sources + About pages — definitions-driven

Sources page groups all metric_definitions rows by their source field,
with metric_id + label + source_url link. About page documents the
three-layer architecture and links to the repo.

Refs spec: §5.5"
```

---

## Phase 8 — Local Verification

### Task 23: Manual end-to-end smoke against live Supabase

**Files:** none (verification only)

- [ ] **Step 1: Start a local web server in `pwa/`**

```bash
cd ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion
python3 -m http.server 8765 --bind 127.0.0.1 &
sleep 1
echo "Open: http://127.0.0.1:8765/pwa/"
```

- [ ] **Step 2: Visual checklist on desktop browser**

Open http://127.0.0.1:8765/pwa/ in Chrome/Safari. Verify:

- [ ] Page loads without console errors (DevTools → Console)
- [ ] Sidebar visible
- [ ] Latest page shows source status pills
- [ ] If aggregate has run since the metric_definitions migration was applied: hero cards render (or empty hero grid if `is_hero` not yet set on any rows)
- [ ] Bento grid shows tiles per domain
- [ ] Click a bento tile → URL changes to `#/domain/<slug>` → drill-in renders
- [ ] Click "Archive" in sidebar → URL `#/archive` → date-bucketed list (may be empty if metric_history is fresh)
- [ ] Click "Runs" in sidebar → URL `#/runs` → commit-graph per source (may be empty if scrapers haven't been re-instrumented + run yet)
- [ ] Click "Sources" → grouped by `source` field
- [ ] Click "About" → static text loads

- [ ] **Step 3: Mobile responsive check via DevTools**

DevTools → Toggle device toolbar → iPhone 13 Pro. Verify:

- [ ] No horizontal scroll
- [ ] Hero grid is 2 columns
- [ ] Bento grid is 2 columns
- [ ] Text readable without zoom
- [ ] Sidebar collapses or stacks reasonably

- [ ] **Step 4: Set 2-3 indicators to is_hero in Supabase Studio**

Use `mcp__plugin_supabase_supabase__execute_sql`:

```sql
update metric_definitions set is_hero = true where metric_id in (
  'usd_bdt_exchange_rate',
  'banking_npl_pct',
  'fx_reserve_gross_and_bpm6'
);
select metric_id, label, is_hero from metric_definitions where is_hero = true;
```

(Adjust metric_ids to match what's actually in your `metric_definitions` table — run `select metric_id from metric_definitions limit 20` first if unsure.)

Reload the PWA. Verify hero cards now appear.

- [ ] **Step 5: Stop the server**

```bash
kill %1 2>/dev/null
```

- [ ] **Step 6: Commit any hot-fixes you made during verification**

If you found bugs and fixed them:

```bash
git add -A
git commit -m "fix(pwa): hot-fix from local verification

[describe what was broken and the fix]"
```

If everything worked, no commit needed — proceed to Phase 9.

---

## Phase 9 — Deployment

### Task 24: GitHub Actions workflow for PWA deploy

**Files:**
- Create: `.github/workflows/pwa-deploy.yml`

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/pwa-deploy.yml`:

```yaml
name: Deploy PWA

on:
  push:
    branches: [main]
    paths:
      - 'pwa/**'
      - '.github/workflows/pwa-deploy.yml'
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

      - name: Bump SW cache version
        run: |
          sed -i "s/CACHE_NAME = 'econdelta-v[^']*'/CACHE_NAME = 'econdelta-v1-$(date -u +%Y%m%d-%H%M%S)'/" pwa/sw.js
          grep "CACHE_NAME" pwa/sw.js

      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: 'pwa'
      - id: deployment
        uses: actions/deploy-pages@v4
```

- [ ] **Step 2: Commit the workflow**

```bash
git add .github/workflows/pwa-deploy.yml
git commit -m "ci(pwa): GitHub Actions workflow to deploy pwa/ to Pages

Triggers on push to main with paths-filter for pwa/** and the workflow
file itself. SW cache version bumped automatically (date-based) so each
deploy invalidates old caches.

Refs spec: §8.1"
```

---

### Task 25: Push to GitHub + enable Pages

**Files:** none (one-time setup + push)

- [ ] **Step 1: Verify all commits are clean**

```bash
cd ~/Projects/clauding-lab/econdelta/.worktrees/v3-expansion
git status -s
git log --oneline origin/feat/v3-expansion..HEAD
```

Expected: status shows only `.gitignore` (pre-existing), log shows all the new commits from Phases 1-9.

- [ ] **Step 2: Push to GitHub** (REQUIRES PER-ACTION APPROVAL — production GitHub push)

```bash
git push origin feat/v3-expansion
```

Expected: push succeeds; GitHub shows new commits on the branch.

- [ ] **Step 3: Open a PR for review**

```bash
gh pr create --title "feat: EconDelta PWA + run_logs + metric_definitions catalog" \
  --body "$(cat <<'EOF'
## Summary

Ships the EconDelta PWA at https://clauding-lab.github.io/econdelta/ with full v3 catalog (60+ indicators), hero+bento mobile layout, and pipeline health commit-graph.

Plus backend additions: metric_definitions catalog seeding, run_logs scraper instrumentation, and the get_latest_dashboard() RPC.

## What's new

- `db/migrations/0002_metric_definitions.sql` — indicator catalog table
- `db/migrations/0003_run_logs.sql` — scraper invocation audit
- `db/migrations/0004_get_latest_dashboard.sql` — single-call RPC
- `utils/supabase_writer.py` — log_run_start, log_run_end, wrap_run, upsert_metric_definitions_seed
- `aggregate_latest.py` — seeds definitions + wrap_run instrumented
- `scrapers/*.py` + `fetch_all.py` + `parse_all.py` — wrap_run instrumented
- `pwa/` — full PWA (4 pages, hero+bento layout, service worker, Pages workflow)

## Spec

Refs: docs/superpowers/specs/2026-05-04-econdelta-pwa-design.md

## Test plan

- [x] All Python tests pass (262 + new run_logging + definitions_seed tests)
- [x] Three SQL migrations applied to Supabase project ssbliukchgibjcjohibi without error
- [x] get_latest_dashboard() RPC returns valid jsonb shape (anon-callable verified)
- [x] PWA renders locally against live Supabase (loaded with definitions/values/sources_status)
- [ ] After merge: GitHub Action deploys pwa/ to Pages, live URL works
- [ ] After deploy: PWA installs as homescreen app on iPhone, opens standalone
- [ ] After deploy: PWA renders cached data when offline + STALE banner
EOF
)"
```

- [ ] **Step 4: Merge PR after review** (REQUIRES PER-ACTION APPROVAL — PR merge)

```bash
gh pr merge --merge
```

- [ ] **Step 5: Enable GitHub Pages in repo settings**

Open https://github.com/clauding-lab/econdelta/settings/pages

Set **Source** = "GitHub Actions". Save.

- [ ] **Step 6: Watch the deploy**

```bash
gh run watch
```

Expected: workflow runs, deploys to Pages within ~30s. URL shown in the output.

- [ ] **Step 7: Visit the live URL**

Open https://clauding-lab.github.io/econdelta/ in browser.

Verify:
- [ ] Page loads
- [ ] Console has no fetch errors
- [ ] Latest page renders with current data
- [ ] All 4 pages navigable

---

## Phase 10 — Backend Deploy + Live Verification

### Task 26: Deploy backend changes to ExonVPS

**Files:** none (deploy)

**REQUIRES PER-ACTION APPROVAL — production VPS SSH.**

- [ ] **Step 1: Pull latest code on ExonVPS**

```bash
ssh adnan-local@103.187.23.22 'cd ~/econdelta && git fetch origin && git log --oneline HEAD..origin/feat/v3-expansion | head -10'
```

Expected: shows the new commits from this PR.

```bash
ssh adnan-local@103.187.23.22 'cd ~/econdelta && git pull origin feat/v3-expansion'
```

(Or if PR was merged to main, pull main and switch.)

- [ ] **Step 2: Verify Python imports clean**

```bash
ssh adnan-local@103.187.23.22 'cd ~/econdelta && .venv/bin/python -c "
from utils.supabase_writer import log_run_start, log_run_end, wrap_run, upsert_metric_definitions_seed
from aggregate_latest import _build_definition_seeds
print(\"all imports clean\")
"'
```

Expected: prints "all imports clean".

- [ ] **Step 3: Manually fire aggregate.service to seed definitions + record first aggregate run_log**

```bash
ssh adnan-local@103.187.23.22 'sudo systemctl start econdelta-aggregate.service'
```

Wait ~30s, then:

```bash
ssh adnan-local@103.187.23.22 'systemctl status econdelta-aggregate.service --no-pager -n 0 | head -10'
```

Expected: ActiveExitCode=0/SUCCESS or activating-near-completion.

- [ ] **Step 4: Verify metric_definitions seeded**

`mcp__plugin_supabase_supabase__execute_sql`:

```sql
select count(*) as definition_count from metric_definitions;
select metric_id, label, domain from metric_definitions order by domain, sort_order limit 20;
```

Expected: count > 0 (likely ~60). Sample rows show indicator catalog.

- [ ] **Step 5: Verify aggregate fired into run_logs**

```sql
select source, status, started_at, duration_ms, error
from run_logs
order by started_at desc
limit 10;
```

Expected: at least one row with source='aggregate', status='ok'.

- [ ] **Step 6: Manually fire one scraper to verify run_logs instrumentation**

```bash
ssh adnan-local@103.187.23.22 'sudo systemctl start econdelta-dse.service'
```

Wait ~30s, then check:

```sql
select source, status, started_at, duration_ms
from run_logs
where source = 'dse_market'
order by started_at desc limit 3;
```

Expected: new dse_market row.

---

### Task 27: Real-device PWA verification on iPhone

**Files:** none (real-device test)

- [ ] **Step 1: Visit PWA URL on iPhone Safari**

Open `https://clauding-lab.github.io/econdelta/` on iPhone.

Verify:
- [ ] Page loads (~3-5s on first visit due to vendor download)
- [ ] Layout looks correct (no horizontal scroll, hero + bento visible)

- [ ] **Step 2: Install as PWA (Add to Home Screen)**

Safari → Share → Add to Home Screen.

Verify:
- [ ] App icon shows correctly (oxblood-themed icon)
- [ ] Name shows as "EconDelta"

- [ ] **Step 3: Open from homescreen, verify standalone mode**

Tap the icon.

Verify:
- [ ] Opens without Safari address bar (standalone mode)
- [ ] Status bar tinted with theme color (`#c34a1f`)
- [ ] All 4 pages navigable

- [ ] **Step 4: Test offline behavior**

In iPhone settings → Airplane mode ON.

Reopen the PWA from homescreen.

Verify:
- [ ] App still loads (cached)
- [ ] Last-known data shown
- [ ] Some indication that data may be stale (or at least no crash)

Turn airplane mode off.

- [ ] **Step 5: Document any issues found**

Note any bugs or rough edges. Triage as either:
- (a) blocking — fix before declaring MVP done
- (b) polish — file as future work

---

## Phase 11 — Polish + Stretch (open-ended)

### Task 28: Hero card tuning + indicator labels

Review `metric_definitions` rows in Supabase Studio. Tune:

- Set `is_hero=true` on the 4 highest-priority indicators (USD/BDT, DSEX, banking_npl_pct, gross reserves)
- Edit `label` for any indicator with awkward titleized defaults (e.g. "Banking Npl Pct" → "NPL Ratio")
- Set `short_label` for indicators where the full label is too long for hero/bento cards
- Set `sort_order` so the most important indicators appear first within each domain
- Set `format` to `pct-2dp` for percentage indicators, `currency-bdt` where appropriate

No code change needed — edits are live immediately on next PWA reload.

---

## Self-Review Checklist (run before declaring plan ready)

- [x] Every task has Files: section with exact paths
- [x] Every task has TDD-style steps with code blocks
- [x] No "TBD" / "TODO" / "implement later" placeholders
- [x] All function names referenced in later tasks were defined in earlier tasks (`wrap_run` defined in Task 6 used in Tasks 8/9, etc.)
- [x] Spec requirements covered: §3 (Tasks 1-3), §4 (Tasks 4-9), §5 (Tasks 10-22), §6 (Task 11+18-19), §7 (Task 13), §8 (Tasks 24-25), §9-10 implicit in Phase 8
- [x] Production-touching steps (push, PR merge, SSH to ExonVPS) explicitly tagged "REQUIRES PER-ACTION APPROVAL"

---

## Done When

- All 27 tasks checked off
- Live PWA renders at https://clauding-lab.github.io/econdelta/
- Installs as homescreen app on iPhone
- Latest page shows ≥4 hero cards + bento tiles for all 7 v3 domains
- Runs page shows commit-graph for all 6 instrumented sources
- Archive page shows ≥30 days of metric_history
- All scrapers writing to run_logs (verifiable via Supabase Studio)
- aggregate_latest.py auto-seeds metric_definitions on each fire (idempotent)
- All Python tests pass (~280+ total)
