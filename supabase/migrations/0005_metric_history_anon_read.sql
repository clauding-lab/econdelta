-- ============================================================================
-- 0005 — metric_history anon read policy
-- ----------------------------------------------------------------------------
-- The EconDelta PWA reads metric_history via get_latest_dashboard() RPC and
-- direct REST queries (90-day archive). The RPC is SECURITY INVOKER, so the
-- caller's RLS context applies. Without an anon SELECT policy on
-- metric_history, the PWA returns empty values{} despite valid data.
--
-- Migration 0001 only added a service_role_all policy. This adds the missing
-- anon-read counterpart. Data exposure is unchanged in practice — the-brief
-- website already renders this data on its public chart pages.
-- ============================================================================

do $$
begin
  if not exists (select 1 from pg_policies
    where schemaname='public' and tablename='metric_history'
      and policyname='anon read history') then
    create policy "anon read history" on public.metric_history
      for select to anon using (true);
  end if;
end $$;

comment on policy "anon read history" on public.metric_history is
  'Public read for the EconDelta PWA + the-brief charts. No PII; data is macro indicators.';
