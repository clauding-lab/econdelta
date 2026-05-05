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
