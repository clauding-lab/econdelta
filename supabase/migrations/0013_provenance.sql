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
--                     'hybrid'        — a mix of both methods in ONE row,
--                                       either direction: a deterministic
--                                       parse with an LLM-recovered field
--                                       (e.g. date recovery on the
--                                       pdf_component / hybrid.parse_one
--                                       fallback path), OR an LLM-extracted
--                                       value with a deterministically-
--                                       recovered field (e.g. a regex-parsed
--                                       date attached to an LLM-read number)
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
-- safe no-op no matter which of the two routes below applies it.
--
-- Apply via (db/README.md's canonical mechanism — this DB is shared with The
-- Brief, so `supabase db push` does NOT work; see "Applying migrations" in
-- that file):
--   supabase db query --linked -f supabase/migrations/0013_provenance.sql
-- from a linked Mac checkout (one-time `supabase link --project-ref <ref>`).
--
-- Alternate, explicitly supported: paste this whole file into the Supabase
-- dashboard SQL editor and run it (no psql on that surface) — the owner may
-- prefer this route on a box with no linked checkout; the statement block is
-- self-contained either way.
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
  'fallback), ''hybrid'' (a mix of both methods in ONE row, either '
  'direction — a deterministic parse with an LLM-recovered field, e.g. '
  'date recovery, OR an LLM-extracted value with a deterministically-'
  'recovered field), or ''manual'' (hand-transcribed / one-off seed). '
  'NULLABLE (migration 0013). Distinct from ``source`` above, which '
  'records the ORIGINATING ORGANIZATION (e.g. BB, DSE, EconDelta) — never '
  'conflate the two. Populated only when the writer passes provenance= AND '
  'ECONDELTA_PROVENANCE_ENABLED=1 is set (see utils/supabase_writer.py). '
  'metric_history_monthly does NOT get this column — its writers are '
  'separate one-off/backfill scripts this change does not touch.';

-- PostgREST caches the schema and only reloads it on its own poll interval or
-- a DDL event trigger — neither is guaranteed to fire promptly for a manual
-- dashboard/db-query apply. Ask it to reload NOW so the API layer (what the
-- writer and the anon-read consumers actually talk to) sees the new column
-- immediately rather than 404/ignoring it until the next poll.
notify pgrst, 'reload schema';

-- ===========================================================================
-- VERIFICATION (run after applying):
--   select column_name, is_nullable, data_type
--     from information_schema.columns
--    where table_schema='public' and table_name='metric_history'
--      and column_name='provenance';                          -- expect 1 row, YES, text
--
--   -- information_schema only proves the DDL landed — it says nothing about
--   -- whether PostgREST's schema CACHE has picked it up. Confirm the API
--   -- layer separately (this is what the writer actually hits):
--   --   curl -s -o /dev/null -w '%{http_code}\n' \
--   --     "$SUPABASE_URL/rest/v1/metric_history?select=provenance&limit=1" \
--   --     -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
--   --     -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"
--   -- Expect 200. A stale schema cache (the notify above didn't take, or
--   -- fired before the DDL committed) returns PGRST204 "Column not found in
--   -- schema cache" even though the information_schema query above is clean.
--
--   select conname from pg_constraint
--    where conname = 'metric_history_provenance_check';        -- expect 1 row
--
--   -- Should reject (run manually, expect an error, then roll back / don't commit):
--   -- insert into metric_history (metric_id, as_of, value, source, provenance)
--   --   values ('test_provenance_guard', current_date, 1, 'test', 'bogus');
-- ===========================================================================
