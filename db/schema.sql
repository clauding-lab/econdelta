-- EconDelta Supabase schema — canonical DDL.
--
-- Source of truth for the metric_history table that EconDelta writes
-- (utils/supabase_writer.py, fired from aggregate_latest.py at 06:10 BDT
-- daily) and consumers (the brief, future apps) read.
--
-- This file mirrors the production schema as it currently exists in the
-- shared brief Supabase project. Changes go through db/migrations/<N>_*.sql
-- with a corresponding pull request — never apply ad-hoc DDL via the
-- Supabase dashboard.
--
-- Apply via:
--   psql "$DATABASE_URL" -f db/schema.sql
--   # or
--   supabase db push

-- =====================================================================
-- metric_history — daily / cadence-aligned readings of every indicator
-- EconDelta scrapes. One row per (metric_id, as_of) tuple. The (metric_id,
-- as_of) pair is the upsert key; ``ingested_at`` records when the row was
-- last written (helps debug retries and discovery latency).
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.metric_history (
    metric_id    text         NOT NULL,
    as_of        date         NOT NULL,
    value        numeric      NOT NULL,
    source       text         NOT NULL,
    ingested_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_id, as_of)
);

-- Indexes for the common consumer query shapes:
--   1. "give me the last N days of metric X"  — ordered by as_of desc
--   2. "what indicators were updated today"   — by as_of (range scan)
CREATE INDEX IF NOT EXISTS metric_history_metric_id_as_of_desc_idx
    ON public.metric_history (metric_id, as_of DESC);

CREATE INDEX IF NOT EXISTS metric_history_as_of_idx
    ON public.metric_history (as_of);

-- =====================================================================
-- Row-Level Security
-- =====================================================================
-- The table is currently service-role-only — there is no public anon
-- read path. Future consumers that don't run on a trusted VPS should:
--   1. get a scoped role added (e.g. ``econdelta_reader``)
--   2. have RLS policies attached to that role
--   3. authenticate with a per-app key, NOT the service role
--
-- Don't drop the service-role write path; it's how EconDelta itself
-- (running on ExonVPS) and the bb/dse builders (transitional) upsert.

ALTER TABLE public.metric_history ENABLE ROW LEVEL SECURITY;

-- Allow the service-role key full access. PostgREST's service_role JWT
-- bypasses RLS by default; this policy is a belt-and-braces redundancy.
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname='public' AND tablename='metric_history'
      AND policyname='service_role_all'
  ) THEN
    CREATE POLICY service_role_all ON public.metric_history
      FOR ALL TO service_role USING (true) WITH CHECK (true);
  END IF;
END $$;

-- =====================================================================
-- Comments — show up in Supabase studio + introspection tools
-- =====================================================================
COMMENT ON TABLE  public.metric_history IS
    'Daily archive of EconDelta indicators. EconDelta @ ExonVPS aggregator '
    'writes here at 06:10 BDT. See econdelta/docs/data-contract.md for '
    'the indicator_id catalog and consumption patterns.';

COMMENT ON COLUMN public.metric_history.metric_id IS
    'Stable indicator identifier. See docs/indicator-catalog.md for the '
    'full enumeration. Once an id is in production it is never renamed; '
    'deprecate-then-add for shape changes.';

COMMENT ON COLUMN public.metric_history.as_of IS
    'The date the reading represents (NOT the date it was scraped). For '
    'monthly indicators this is the month-end of the reporting period; '
    'for quarterly the quarter-end; for daily the trading/business day.';

COMMENT ON COLUMN public.metric_history.value IS
    'Numeric value in the unit declared for this indicator in '
    'sources-v3.json (e.g. percent, BDT crore, USD billion). Bool, '
    'string, and dict values are filtered by the writer and never land '
    'here — see utils/supabase_writer.py:_rows_from_data.';

COMMENT ON COLUMN public.metric_history.source IS
    'Origin of the reading. ``EconDelta`` for rows written by the daily '
    'aggregator (the canonical writer). ``BB``, ``BBS``, etc. for legacy '
    'rows written by the brief''s now-removed inline upserts. New writers '
    'should use ``EconDelta`` unless they have a strong reason otherwise.';

COMMENT ON COLUMN public.metric_history.ingested_at IS
    'Write-liveness timestamp, POSTED BY THE CLIENT on every upsert (the '
    'column default now() fires only on INSERT, never on the UPDATE half of '
    'a merge-upsert — see utils/supabase_writer.py:_rows_from_data, E1.1). '
    'Diagnostics only — consumers should order by ``as_of``, not '
    '``ingested_at``.';

-- ============================================================================
-- 0008 — briefings
-- ----------------------------------------------------------------------------
-- One row per weekly ALCO briefing, generated Monday morning by a Claude
-- Opus session on ExonVPS (see briefing/ package). Powers YieldScope's
-- Briefings page: the weekly read, the curated anomaly list, and the
-- prior-week history. `open_threads` is the job's persistent memory —
-- carried forward into next week's prompt.
-- Written by the briefing job under service_role; read by the PWA under anon.
-- ============================================================================

create table if not exists public.briefings (
  week_of            date primary key,                 -- Monday's date (ISO week anchor)
  generated_at       timestamptz not null default now(),
  title              text not null,
  body               text not null,
  featured_anomalies jsonb not null default '[]'::jsonb, -- [{candidate_id,label,stat,value,detail,severity,metric_id,why}]
  open_threads       jsonb not null default '[]'::jsonb, -- [{id,thread,status,since_week,note}]
  data_as_of         date not null,                    -- freshness stamp for the honesty banner
  stale_series       text[] not null default '{}',     -- peripheral metric_ids flagged stale this run
  model              text not null,                    -- e.g. 'opus[1m]'
  effort             text not null,                    -- e.g. 'xhigh'
  total_cost_usd     numeric,
  inputs_hash        text
);

create index if not exists briefings_week_of_idx on public.briefings (week_of desc);

alter table public.briefings enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies
    where schemaname='public' and tablename='briefings' and policyname='service_role_all') then
    create policy service_role_all on public.briefings
      for all to service_role using (true) with check (true);
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_policies
    where schemaname='public' and tablename='briefings' and policyname='anon read briefings') then
    create policy "anon read briefings" on public.briefings
      for select to anon using (true);
  end if;
end $$;

comment on policy "anon read briefings" on public.briefings is
  'Public read for the YieldScope Briefings page. No PII; macro commentary only.';

-- ============================================================================
-- 0009 — auction_results + auction_calendar
-- ----------------------------------------------------------------------------
-- Structured (row-shaped) BB primary-auction storage. metric_history is
-- scalar-numeric-only and the writer drops dict/list payloads, so an auction
-- print (multi-row, multi-field) needs real row tables. Two distinct shapes:
-- results = auctions that happened (size/bid/cover/wam/cutoff); calendar =
-- forward scheduled issuance (notional only — the four result fields do not
-- exist for an un-held auction). Upsert key (auction_date, tenor) on both.
-- Written by EconDelta under service_role; read by the PWA under anon.
-- ============================================================================

create table if not exists public.auction_results (
  auction_date  date         not null,            -- the day the auction was held
  tenor         text         not null,            -- e.g. '91d', '182d', '364d', '5y', '10y'
  size          numeric,                           -- accepted/issued amount (BDT crore)
  bid           numeric,                           -- total bid amount (BDT crore)
  cover         numeric,                           -- bid-to-cover ratio (bid / accepted)
  wam           numeric,                           -- weighted-average maturity (years)
  cutoff        numeric,                           -- cut-off / weighted-average yield (percent)
  ingested_at   timestamptz  not null default now(),
  primary key (auction_date, tenor)
);

create index if not exists auction_results_date_desc_idx
  on public.auction_results (auction_date desc);

alter table public.auction_results enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies
    where schemaname='public' and tablename='auction_results' and policyname='service_role_all') then
    create policy service_role_all on public.auction_results
      for all to service_role using (true) with check (true);
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_policies
    where schemaname='public' and tablename='auction_results' and policyname='anon read auction_results') then
    create policy "anon read auction_results" on public.auction_results
      for select to anon using (true);
  end if;
end $$;

comment on table public.auction_results is
  'Per-print BB primary-auction RESULTS (held auctions). One row per '
  '(auction_date, tenor). Written by EconDelta @ ExonVPS; read by the '
  'YieldScope PWA under anon.';
comment on policy "anon read auction_results" on public.auction_results is
  'Public read for the YieldScope auction panels. No PII; macro auction data only.';

create table if not exists public.auction_calendar (
  auction_date  date         not null,            -- the day the auction is SCHEDULED for
  tenor         text         not null,            -- e.g. '91d', '182d', '364d', '5y', '10y'
  notional      numeric,                           -- planned issuance amount (BDT crore)
  ingested_at   timestamptz  not null default now(),
  primary key (auction_date, tenor)
);

create index if not exists auction_calendar_date_idx
  on public.auction_calendar (auction_date);

alter table public.auction_calendar enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies
    where schemaname='public' and tablename='auction_calendar' and policyname='service_role_all') then
    create policy service_role_all on public.auction_calendar
      for all to service_role using (true) with check (true);
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_policies
    where schemaname='public' and tablename='auction_calendar' and policyname='anon read auction_calendar') then
    create policy "anon read auction_calendar" on public.auction_calendar
      for select to anon using (true);
  end if;
end $$;

comment on table public.auction_calendar is
  'Forward (scheduled, not-yet-held) BB auction issuance strip. One row per '
  '(auction_date, tenor) with planned notional only — NO bid/cover/wam/cutoff. '
  'Written by EconDelta; read by the YieldScope PWA under anon.';
comment on policy "anon read auction_calendar" on public.auction_calendar is
  'Public read for the YieldScope Fiscal 12-week issuance strip. No PII.';
