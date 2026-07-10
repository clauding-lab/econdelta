-- 0012_freshness_contract_e31.sql — the freshness & vintage contract in the DB (E3.1).
--
-- Gives all three consumers (The Brief, YieldScope, EconDelta PWA) ONE surface to
-- read staleness from — `v_metric_freshness` — instead of each hand-rolling its
-- own cadence math (unblocks The Brief B3.13). Canonical rules live in
-- docs/data-contract.md §10; this file is the DDL for those rules.
--
-- ✅ STATUS: ALREADY APPLIED to the shared prod DB (verified live 2026-07-10 via
--    pg_get_viewdef, pg_policies, and the grace_days seeding — all match this file
--    exactly). This file is committed as the TRACKED RECORD of that DDL; the DB
--    had it before the repo did. Every block is IDEMPOTENT, so re-running is a
--    safe no-op — but nothing here needs to be applied again.
--
-- If ever reconstructing from scratch (shared DB — NEVER `supabase db push`):
--   supabase db query --linked -f supabase/migrations/0012_freshness_contract_e31.sql
-- then apply the appendix Blocks 4 & 5 interactively (see bottom). Blocks 1–3 are
-- purely additive (new columns, a new view, deprecation flags); Blocks 4 (anon
-- policy dedupe) and 5 (IMF projection split) live in the appendix because Block 4
-- needs a live pg_policies pre-check before any DROP.

-- ---------------------------------------------------------------------------
-- Block 1 — grace_days columns + cadence-seeded defaults.
-- ---------------------------------------------------------------------------
alter table metric_definitions          add column if not exists grace_days integer;
alter table metric_definitions_monthly  add column if not exists grace_days integer;

update metric_definitions set grace_days = case cadence
    when 'daily'       then 4    -- 2 trading days + weekend cushion (view is calendar-day)
    when 'weekly'      then 10
    when 'monthly'     then 45
    when 'quarterly'   then 165
    when 'fiscal_year' then 400
    else grace_days end
 where grace_days is null;

update metric_definitions_monthly set grace_days = coalesce(grace_days, 45)
 where grace_days is null;

-- ---------------------------------------------------------------------------
-- Block 2 — the v_metric_freshness view (over BOTH tables, future-excluded).
--
-- metric_definitions_monthly has NO `cadence` column (verified 2026-07-09) — every
-- id in the monthly system is monthly by construction, so the view infers
-- 'monthly' from the presence of a monthly-definition row (mirrors the sentinel's
-- resolve_cadence fallback). Do NOT reference dm.cadence — it does not exist and
-- CREATE VIEW would fail.
-- ---------------------------------------------------------------------------
create or replace view v_metric_freshness as
with per_table as (
    select metric_id,
           max(as_of) filter (where as_of <= current_date) as latest_as_of,
           max(ingested_at)                                 as latest_ingested_at
    from metric_history group by metric_id
    union all
    select metric_id,
           max(as_of) filter (where as_of <= current_date),
           max(ingested_at)
    from metric_history_monthly group by metric_id
),
agg as (
    select metric_id,
           max(latest_as_of)       as latest_as_of,
           max(latest_ingested_at) as latest_ingested_at
    from per_table group by metric_id
)
select a.metric_id,
       a.latest_as_of,
       a.latest_ingested_at,
       coalesce(d.cadence,
                case when dm.metric_id is not null then 'monthly' end) as cadence,
       coalesce(d.grace_days, dm.grace_days) as grace_days,
       (current_date - a.latest_as_of)       as age_days,
       (a.latest_as_of >= current_date - coalesce(d.grace_days, dm.grace_days)) as is_fresh
from agg a
left join metric_definitions         d  on d.metric_id  = a.metric_id
left join metric_definitions_monthly dm on dm.metric_id = a.metric_id;

grant select on v_metric_freshness to anon;

-- grace_days is null (no definition row) ⇒ is_fresh is null = "unknown"; this
-- surfaces the ~100 live metric_ids with no metric_definitions row (a real
-- coverage gap — back-filling those definitions is a follow-up).

-- ---------------------------------------------------------------------------
-- Block 3 — deprecate/alias the frozen legacy ids (all verified frozen
-- 2026-07-09: ingested_at stopped weeks ago and a superseding id is live).
-- Rows are KEPT (owner decision Option A — point-in-time history); consumers
-- filter `where not deprecated`.
-- ---------------------------------------------------------------------------
alter table metric_definitions add column if not exists deprecated boolean default false;
alter table metric_definitions add column if not exists alias_of  text;

update metric_definitions d set deprecated = true, alias_of = v.alias_of
from (values
    ('dse_dsex_close',                'dsex'),
    ('policy_rate_slf_sdf',           'policy_rate_sdf'),    -- superseded by the repo/sdf/slf split (PR #30)
    ('nbr_fytd_collected_tbs',        'tax_revenue'),        -- news scrapers retired (landmine 4)
    ('nbr_fytd_collected_dailystar',  'tax_revenue'),
    ('bb_gross_reserves',             'gross_reserves_usd_bn'),
    ('comm_lng_jkm',                  'lng_price_usd_mmbtu')
) as v(metric_id, alias_of)
where d.metric_id = v.metric_id;

-- ===========================================================================
-- VERIFICATION (run after applying Blocks 1–3):
--   select cadence, count(*), min(grace_days), max(grace_days)
--     from metric_definitions group by cadence;                       -- grace seeded
--   select * from v_metric_freshness where is_fresh = false
--     order by age_days desc limit 30;                                -- current breaches
--   select metric_id, alias_of from metric_definitions where deprecated;  -- marked
--
-- ===========================================================================
-- APPENDIX — INTERACTIVE-ONLY snippets. ✅ Both already applied (verified live
-- 2026-07-10: each history table now has exactly one anon SELECT policy, and the
-- 6 debt_gdp_ratio projection rows already moved to debt_gdp_ratio_proj). Retained
-- here as the record. If ever re-running: DO NOT run via `-f`; paste into the SQL
-- editor by hand and read the Block-4 pre-check output before any DROP.
--
-- Block 4 — drop the duplicate anon SELECT policies (each history table carries
-- two identical ones; keep the canonically-named one). MANDATORY pre-check first:
--
--   -- Pre-check: expect exactly two anon SELECT policies per history table.
--   select tablename, policyname, roles::text, cmd
--     from pg_policies
--    where tablename in ('metric_history','metric_history_monthly')
--    order by tablename, policyname;
--
--   -- Only if the pre-check shows TWO anon SELECT policies on each table:
--   drop policy if exists "anon read history"                on metric_history;
--   drop policy if exists "anon read metric_history_monthly" on metric_history_monthly;
--
--   -- Post-check: each table must STILL have exactly one anon SELECT policy.
--   select tablename, count(*) as anon_select_policies
--     from pg_policies
--    where tablename in ('metric_history','metric_history_monthly')
--      and roles::text like '%anon%' and cmd = 'SELECT'
--    group by tablename;   -- expect 1 and 1
--
-- Block 5 (optional cleanliness — the view already filters future as_of): split
-- the 6 IMF projection rows off debt_gdp_ratio so no "latest" read can touch them.
--
--   update metric_history set metric_id = 'debt_gdp_ratio_proj'
--    where metric_id = 'debt_gdp_ratio' and as_of > current_date;
-- ===========================================================================
