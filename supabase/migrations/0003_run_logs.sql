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
