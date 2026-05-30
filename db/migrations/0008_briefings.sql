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
