-- ============================================================================
-- 0013 — provenance (extraction-method) column for metric_history
-- ----------------------------------------------------------------------------
-- Adds a NULLABLE `provenance` column recording HOW a value was pulled out
-- of its source document: 'deterministic' | 'llm' | 'hybrid' | 'manual'.
--
-- DO NOT CONFLATE with `source` (migration 0001, NOT NULL):
--   `source`      = the ORIGINATING ORGANIZATION the reading comes from
--                   (e.g. 'EconDelta', 'BB', 'DSE Day End Archive').
--   `provenance`  = the EXTRACTION METHOD used to get the value out of that
--                   source document:
--                     'deterministic' — regex / table parser, no LLM call
--                     'llm'           — Claude extraction pass (e.g. the FSR
--                                       NPL-structure LLM extraction)
--                     'hybrid'        — deterministic parse with an
--                                       LLM-recovered field (e.g. date
--                                       recovery on the pdf_component /
--                                       hybrid.parse_one fallback path)
--                     'manual'        — hand-transcribed / one-off seed
--                                       (e.g. scripts/seed_npl_structure.py)
-- These answer two different questions ("whose number is this" vs. "how did
-- we get it out of the document") and must never be merged into one column
-- or read as synonyms of each other.
--
-- NULLABLE by design: existing rows, and any row written before a caller
-- opts in, carry NULL — "extraction method not recorded", not "unknown but
-- required". The writer (utils/supabase_writer.py) only includes this key in
-- the upsert payload when BOTH an explicit `provenance=` was passed AND the
-- env flag ECONDELTA_PROVENANCE_ENABLED=1 is set — see that file for why: the
-- column does not exist in the live DB until this migration is applied, so
-- the write path must stay merge-safe against the pre-migration schema until
-- then (a PostgREST payload carrying an unknown column 400s the whole batch).
--
-- Scope: `metric_history` ONLY, not `metric_history_monthly`. The two tables
-- are NOT symmetrical for this concept (checked before writing this
-- migration): `upsert_metric_history` — the sole call site this PR stamps —
-- writes only to `metric_history`. `metric_history_monthly` is fed by
-- separate, siloed writers (scripts/seed_macro_monthly.py,
-- scripts/backfill_fiscal.py, scripts/backfill_call_money_monthly.py; see
-- AGENTS.md landmine 20, "two parallel metric systems — don't mix
-- namespaces"), none of which this PR touches. Adding an unpopulated column
-- to the monthly table now would be speculative; add it in a follow-up if
-- and when a monthly writer actually stamps provenance.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS + a guarded ADD CONSTRAINT — Postgres
-- has no "ADD CONSTRAINT IF NOT EXISTS"), so re-running this whole file is a
-- safe no-op. Single self-contained statement block — paste-and-run in the
-- Supabase dashboard SQL editor (no psql access there).
--
-- Apply via: paste this whole file into the Supabase SQL editor and run it.
--   (Or, from a linked Mac checkout:
--     supabase db query --linked -f supabase/migrations/0013_provenance.sql)
-- ============================================================================

alter table public.metric_history
  add column if not exists provenance text;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'metric_history_provenance_check'
      and conrelid = 'public.metric_history'::regclass
  ) then
    alter table public.metric_history
      add constraint metric_history_provenance_check
      check (provenance in ('deterministic', 'llm', 'hybrid', 'manual'));
  end if;
end $$;

comment on column public.metric_history.provenance is
  'Extraction method used to pull this value out of its source document: '
  '''deterministic'' (regex/table parser), ''llm'' (Claude extraction '
  'fallback), ''hybrid'' (deterministic parse + LLM-recovered field, e.g. '
  'date recovery), or ''manual'' (hand-transcribed / one-off seed). '
  'NULLABLE. Distinct from `source` (migration 0001, NOT NULL), which '
  'records the ORIGINATING ORGANIZATION (e.g. BB, DSE, EconDelta) — never '
  'conflate the two. Populated only when the writer passes provenance= AND '
  'ECONDELTA_PROVENANCE_ENABLED=1 is set (see utils/supabase_writer.py).';

-- ===========================================================================
-- VERIFICATION (run after applying):
--   select column_name, is_nullable, data_type
--     from information_schema.columns
--    where table_schema='public' and table_name='metric_history'
--      and column_name='provenance';                          -- expect 1 row, YES, text
--
--   select conname from pg_constraint
--    where conname = 'metric_history_provenance_check';        -- expect 1 row
--
--   -- Should reject (run manually, expect an error, then roll back / don't commit):
--   -- insert into metric_history (metric_id, as_of, value, source, provenance)
--   --   values ('test_provenance_guard', current_date, 1, 'test', 'bogus');
-- ===========================================================================
