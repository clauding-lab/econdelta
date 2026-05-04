-- EconDelta Supabase schema — canonical DDL.
--
-- Source of truth for the metric_history table that EconDelta writes
-- (utils/supabase_writer.py, fired from aggregate_latest.py at 06:10 BDT
-- daily) and consumers (the brief, future apps) read.
--
-- This file mirrors the production schema as it currently exists in the
-- shared brief Supabase project. Changes go through db/migrations/<N>_*.sql
-- with a corresponding pull request — never apply ad-hoc DDL via the
-- Supabase dashboard.
--
-- Apply via:
--   psql "$DATABASE_URL" -f db/schema.sql
--   # or
--   supabase db push

-- =====================================================================
-- metric_history — daily / cadence-aligned readings of every indicator
-- EconDelta scrapes. One row per (metric_id, as_of) tuple. The (metric_id,
-- as_of) pair is the upsert key; ``ingested_at`` records when the row was
-- last written (helps debug retries and discovery latency).
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.metric_history (
    metric_id    text         NOT NULL,
    as_of        date         NOT NULL,
    value        numeric      NOT NULL,
    source       text         NOT NULL,
    ingested_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_id, as_of)
);

-- Indexes for the common consumer query shapes:
--   1. "give me the last N days of metric X"  — ordered by as_of desc
--   2. "what indicators were updated today"   — by as_of (range scan)
CREATE INDEX IF NOT EXISTS metric_history_metric_id_as_of_desc_idx
    ON public.metric_history (metric_id, as_of DESC);

CREATE INDEX IF NOT EXISTS metric_history_as_of_idx
    ON public.metric_history (as_of);

-- =====================================================================
-- Row-Level Security
-- =====================================================================
-- The table is currently service-role-only — there is no public anon
-- read path. Future consumers that don't run on a trusted VPS should:
--   1. get a scoped role added (e.g. ``econdelta_reader``)
--   2. have RLS policies attached to that role
--   3. authenticate with a per-app key, NOT the service role
--
-- Don't drop the service-role write path; it's how EconDelta itself
-- (running on ExonVPS) and the bb/dse builders (transitional) upsert.

ALTER TABLE public.metric_history ENABLE ROW LEVEL SECURITY;

-- Allow the service-role key full access. PostgREST's service_role JWT
-- bypasses RLS by default; this policy is a belt-and-braces redundancy.
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname='public' AND tablename='metric_history'
      AND policyname='service_role_all'
  ) THEN
    CREATE POLICY service_role_all ON public.metric_history
      FOR ALL TO service_role USING (true) WITH CHECK (true);
  END IF;
END $$;

-- =====================================================================
-- Comments — show up in Supabase studio + introspection tools
-- =====================================================================
COMMENT ON TABLE  public.metric_history IS
    'Daily archive of EconDelta indicators. EconDelta @ ExonVPS aggregator '
    'writes here at 06:10 BDT. See econdelta/docs/data-contract.md for '
    'the indicator_id catalog and consumption patterns.';

COMMENT ON COLUMN public.metric_history.metric_id IS
    'Stable indicator identifier. See docs/indicator-catalog.md for the '
    'full enumeration. Once an id is in production it is never renamed; '
    'deprecate-then-add for shape changes.';

COMMENT ON COLUMN public.metric_history.as_of IS
    'The date the reading represents (NOT the date it was scraped). For '
    'monthly indicators this is the month-end of the reporting period; '
    'for quarterly the quarter-end; for daily the trading/business day.';

COMMENT ON COLUMN public.metric_history.value IS
    'Numeric value in the unit declared for this indicator in '
    'sources-v3.json (e.g. percent, BDT crore, USD billion). Bool, '
    'string, and dict values are filtered by the writer and never land '
    'here — see utils/supabase_writer.py:_rows_from_data.';

COMMENT ON COLUMN public.metric_history.source IS
    'Origin of the reading. ``EconDelta`` for rows written by the daily '
    'aggregator (the canonical writer). ``BB``, ``BBS``, etc. for legacy '
    'rows written by the brief''s now-removed inline upserts. New writers '
    'should use ``EconDelta`` unless they have a strong reason otherwise.';

COMMENT ON COLUMN public.metric_history.ingested_at IS
    'Server-side timestamp of the last upsert. Diagnostics only — '
    'consumers should order by ``as_of``, not ``ingested_at``.';
