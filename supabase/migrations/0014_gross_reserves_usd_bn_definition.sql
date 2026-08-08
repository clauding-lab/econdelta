-- 0014_gross_reserves_usd_bn_definition.sql — backfill the missing catalog row
-- for the dual-write reserves id `gross_reserves_usd_bn`.
-- ----------------------------------------------------------------------------
-- STATUS: NOT YET APPLIED to the shared prod DB as of this commit. Applying
-- this file to Supabase is a POST-MERGE, OWNER-APPROVED step — this PR ships
-- the migration only (AGENTS.md: supabase/migrations/ changes need explicit
-- sign-off before hitting the shared, Brief-shared database). Apply with:
--   supabase db query --linked -f supabase/migrations/0014_gross_reserves_usd_bn_definition.sql
--
-- Background:
-- `aggregate_latest.py` deliberately dual-writes one BB reserves read under
-- TWO metric_history ids: `gross_reserves_usd_bn` (the pre-existing daily-
-- namespace id, set directly from bb_forex.py's scrape — see flatten_data
-- around line ~202 and the monthly-alias block around line ~747) and
-- `fx_reserve_gross_and_bpm6` (a sources-v3.json config-driven id, copied
-- FROM gross_reserves_usd_bn at aggregate_latest.py ~line 756). Do not
-- "simplify" this into a single id — the dual write is intentional (see
-- AGENTS.md landmine 44 / the reserves-memo D5 split).
--
-- Only the config-driven twin ever gets a metric_definitions row: it comes
-- from `_build_definition_seeds`, which reads sources-v3.json indicators —
-- `gross_reserves_usd_bn` has no sources-v3.json entry (it's a scraper-only
-- id, like dsex / usd_bdt_mid), so it was NEVER seeded. Confirmed live
-- 2026-08-08: metric_history has rows for gross_reserves_usd_bn, but
-- metric_definitions returns [] for it. Migration 0012's v_metric_freshness
-- view already flagged this general shape of gap ("grace_days is null (no
-- definition row) => is_fresh is null = 'unknown'... a real coverage gap —
-- back-filling those definitions is a follow-up") — this is that follow-up
-- for this one id.
--
-- This row mirrors the canonical fx_reserve_gross_and_bpm6 definition
-- (domain, cadence=monthly, grace_days=45 per migration 0012's monthly rule)
-- so v_metric_freshness can classify gross_reserves_usd_bn as fresh/stale
-- instead of returning NULL, and so the EconDelta PWA / get_latest_dashboard()
-- has a label for it instead of falling back to the bare metric_id.
--
-- Idempotent: ON CONFLICT (metric_id) DO NOTHING, matching the aggregator's
-- own seeding convention (migration 0002) — a manual Studio edit after this
-- lands is preserved forever, same as every other definitions row.

INSERT INTO metric_definitions (
    metric_id,
    label,
    short_label,
    unit,
    domain,
    sort_order,
    cadence,
    format,
    description,
    source,
    source_url,
    is_hero,
    inverted,
    grace_days,
    deprecated,
    alias_of
) VALUES (
    'gross_reserves_usd_bn',
    'Gross Reserves Usd Bn',
    NULL,
    NULL,
    'forex_and_reserves',
    100,
    'monthly',
    'comma-2dp',
    'Dual-write twin of fx_reserve_gross_and_bpm6 — both ids are written from '
        || 'the SAME bb_forex.py reserves read in aggregate_latest.py (see '
        || 'AGENTS.md landmine 44). gross_reserves_usd_bn is the pre-existing '
        || 'daily-namespace id (no sources-v3.json entry, so it never got an '
        || 'automatic definitions row); fx_reserve_gross_and_bpm6 is the '
        || 'sources-v3.json config-driven id copied from it. Same source, '
        || 'same value, same cadence — do not treat these as independent '
        || 'confirmation of anything.',
    NULL,
    'https://www.bb.org.bd/en/index.php/publication/publictn/5/27',
    false,
    false,
    45,
    false,
    NULL
)
ON CONFLICT (metric_id) DO NOTHING;

-- ===========================================================================
-- VERIFICATION (run after applying):
--   select metric_id, cadence, grace_days, domain
--     from metric_definitions where metric_id = 'gross_reserves_usd_bn';
--   -- expect one row: cadence='monthly', grace_days=45, domain='forex_and_reserves'
--
--   select * from v_metric_freshness where metric_id = 'gross_reserves_usd_bn';
--   -- expect is_fresh to resolve to true/false, not NULL
-- ===========================================================================
