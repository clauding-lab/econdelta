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
