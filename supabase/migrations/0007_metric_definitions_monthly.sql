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
