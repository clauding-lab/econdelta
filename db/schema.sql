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
-- RLS: anon-read IS enabled on this table (verified live 2026-07-09 via
-- pg_policies — TWO anon SELECT policies exist, ``anon_read_metric_history``
-- plus a legacy duplicate ``anon read history``; the duplicate is a dedupe
-- candidate — see docs/data-contract.md). So the PWA reads with the ANON key.
-- This SUPERSEDES the old "service-role-only, no anon read path" note here and
-- in AGENTS.md landmine 18. EconDelta (ExonVPS), the briefing job, and the
-- freshness sentinel still use the SERVICE ROLE for run_logs and other
-- non-anon tables.
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
    'Data-contract rule (docs/data-contract.md, Option A): ``as_of`` is the '
    'source''s reporting vintage and never advances without republication, so '
    'ORDER BY as_of gives the correct data VINTAGE. A consumer that needs '
    'WRITE-liveness for a vintage-stamped id (is the pipeline still writing?) '
    'reads latest-by-``ingested_at``; the ``v_metric_freshness`` view (E3.1) is '
    'the canonical freshness surface for all three consumers.';

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

-- ============================================================================
-- metric_definitions — display metadata for the DAILY metric system
-- ----------------------------------------------------------------------------
-- Canonical snapshot of the LIVE table (verified via information_schema,
-- 2026-07-09). Seeded idempotently (ON CONFLICT DO NOTHING) by
-- aggregate_latest._build_definition_seeds → upsert_metric_definitions_seed;
-- first insert wins so Studio hand-edits are preserved. Consumers join it for
-- labels/units/cadence; the E3.1 package adds grace_days + deprecated/alias_of
-- (see docs/data-contract.md §10.3 — DDL applied via Adnan's SQL editor only).
-- ============================================================================
create table if not exists public.metric_definitions (
  metric_id    text         primary key,
  label        text         not null,
  short_label  text,
  unit         text,
  domain       text         not null,
  sort_order   integer      not null default 100,
  cadence      text,                              -- daily|weekly|monthly|quarterly|fiscal_year
  format       text         default 'comma-2dp',
  description  text,
  source       text,
  source_url   text,
  is_hero      boolean      default false,
  inverted     boolean      default false,
  created_at   timestamptz  not null default now(),
  updated_at   timestamptz  not null default now()
  -- E3.1 package (prepared, not yet applied) adds:
  --   grace_days integer, deprecated boolean default false, alias_of text
);

-- ============================================================================
-- metric_definitions_monthly — display metadata for the MONTHLY metric system
-- ----------------------------------------------------------------------------
-- Canonical snapshot of the LIVE table (verified 2026-07-09). NOTE the shape
-- differences vs metric_definitions: the label column is ``display_name`` and
-- there is NO ``cadence`` column — every id in the monthly system is monthly
-- by construction (ids suffixed _monthly, landmine 20), so any cadence-aware
-- consumer (e.g. v_metric_freshness) must INFER 'monthly' from the row's
-- presence, never reference a cadence column here.
-- ============================================================================
create table if not exists public.metric_definitions_monthly (
  metric_id           text         primary key,
  display_name        text         not null,
  unit                text         not null,
  source_url          text,
  source_attribution  text,
  domain              text         not null,
  description         text,
  notes               text,
  created_at          timestamptz  not null default now(),
  updated_at          timestamptz  not null default now()
  -- E3.1 package (prepared, not yet applied) adds: grace_days integer
);

-- ============================================================================
-- get_latest_dashboard() — the PWA's single-call dashboard RPC
-- ----------------------------------------------------------------------------
-- Canonical snapshot of the LIVE function (pg_get_functiondef, 2026-07-09).
-- Returns definitions + per-metric latest {value, as_of} + per-source run
-- status in one jsonb. NOTE: ``values`` picks latest by max(as_of) with NO
-- future-date filter — debt_gdp_ratio's IMF projection rows (as_of out to
-- 2031-12-31) surface here until Block 5 of the E3.1 package splits them to
-- their own id (docs/data-contract.md §10.3). Freshness must NOT be derived
-- from this RPC — that is v_metric_freshness's job.
-- ============================================================================
create or replace function public.get_latest_dashboard()
returns jsonb
language sql
stable
as $function$
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
          'as_of', as_of
        )
      ), '{}'::jsonb)
      from (
        select distinct on (metric_id) metric_id, value, as_of
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
$function$;
