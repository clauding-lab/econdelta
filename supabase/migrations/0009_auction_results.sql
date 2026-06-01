-- ============================================================================
-- 0009 — auction_results + auction_calendar
-- ----------------------------------------------------------------------------
-- Structured (row-shaped) storage for BB primary-auction data, which
-- metric_history cannot hold: metric_history is scalar-numeric-only (one
-- numeric value per (metric_id, as_of)), and the writer drops dict/list
-- payloads (utils/supabase_writer.py:_rows_from_data). An auction print is a
-- multi-row, multi-field record — it needs a real row table.
--
-- TWO tables, because results and the forward calendar are DIFFERENT shapes:
--
--   auction_results  — per-print RESULTS for auctions that HAVE happened.
--     {auction_date, tenor, size, bid, cover, wam, cutoff}. ~6 rows per print.
--     Fed by the S9 results scraper (BB press releases). Consumed by
--     YieldScope Panels A + B (the Yields table + the Dashboard auction list).
--
--   auction_calendar — the forward 12-week ISSUANCE strip for auctions that
--     have NOT happened yet. {auction_date, tenor, notional} ONLY — bid /
--     cover / wam / cutoff do not exist for an un-held auction, so forcing the
--     calendar into auction_results would leave those four columns NULL on
--     every calendar row and blur "scheduled" vs "happened". Fed by S9's
--     gsec_auction multi-row extension. Consumed by YieldScope Panel C (the
--     Fiscal 12-week strip).
--
-- Both written by EconDelta under service_role; both read by the PWA under
-- the shared Brief anon key — mirroring the metric_history service_role-write
-- / anon-read split (migrations 0001 + 0005).
--
-- Upsert key is (auction_date, tenor) on BOTH tables: one print per tenor per
-- auction day. Writer uses on_conflict=auction_date,tenor + merge-duplicates
-- (utils/supabase_writer.upsert_auction_rows).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- auction_results — per-print RESULTS (auctions that have happened)
-- ----------------------------------------------------------------------------
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
  'YieldScope PWA under anon. See econdelta/docs/indicator-catalog.md.';
comment on column public.auction_results.tenor is
  'Instrument tenor as a label, e.g. 91d / 182d / 364d / 5y / 10y.';
comment on column public.auction_results.size is
  'Accepted / issued amount in BDT crore.';
comment on column public.auction_results.cover is
  'Bid-to-cover ratio (total bid / accepted). Dimensionless.';
comment on column public.auction_results.wam is
  'Weighted-average maturity in years.';
comment on column public.auction_results.cutoff is
  'Cut-off / weighted-average yield, percent.';
comment on policy "anon read auction_results" on public.auction_results is
  'Public read for the YieldScope auction panels. No PII; macro auction data only.';

-- ----------------------------------------------------------------------------
-- auction_calendar — forward 12-week ISSUANCE strip (auctions not yet held)
-- ----------------------------------------------------------------------------
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
  '(auction_date, tenor) with planned notional only — NO bid/cover/wam/cutoff '
  '(those do not exist for an un-held auction). Written by EconDelta; read by '
  'the YieldScope PWA under anon.';
comment on column public.auction_calendar.notional is
  'Planned issuance amount in BDT crore for a scheduled auction.';
comment on policy "anon read auction_calendar" on public.auction_calendar is
  'Public read for the YieldScope Fiscal 12-week issuance strip. No PII.';

-- ============================================================================
-- Down-migration (rollback) — drop both tables. No metric_history impact.
-- Apply manually only when reverting this PR's schema change:
--
--   DROP TABLE IF EXISTS public.auction_results;
--   DROP TABLE IF EXISTS public.auction_calendar;
-- ============================================================================
