-- ============================================================================
-- 0004 — get_latest_dashboard()
-- ----------------------------------------------------------------------------
-- Single-call RPC for the PWA's Latest page. Returns one jsonb blob with:
--   updated_at      — server now() at call time
--   definitions     — array of all metric_definitions rows, sorted (domain, sort_order)
--   values          — { metric_id: {value, as_of} } from latest row per
--                     metric_id in metric_history. Note: as_of carries the
--                     publication date directly for slow-cadence metrics
--                     (FSAR/DAM/NBR) via writer override; cadence-aware
--                     staleness display lives in metric_definitions.cadence.
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
$$;

grant execute on function get_latest_dashboard() to anon;

comment on function get_latest_dashboard() is
  'Single-call dashboard payload for the EconDelta PWA Latest page. Anon-callable.';
