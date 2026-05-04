-- 0001_metric_history.sql — canonical schema for the metric_history table.
--
-- Initial migration. The table already exists in the shared brief Supabase
-- project (created ad-hoc when the brief's bb.py first started upserting
-- bb_gross_reserves rows in April 2026). This migration is idempotent
-- (CREATE TABLE IF NOT EXISTS, DO $$ blocks for policies) so applying it
-- against the existing instance is a no-op except for any indexes /
-- comments / RLS policies that hadn't been added yet.
--
-- Every future schema change goes here as a new numbered file.
--
-- Apply via:
--   psql "$DATABASE_URL" -f db/migrations/0001_metric_history.sql
--   # or
--   supabase migration up

CREATE TABLE IF NOT EXISTS public.metric_history (
    metric_id    text         NOT NULL,
    as_of        date         NOT NULL,
    value        numeric      NOT NULL,
    source       text         NOT NULL,
    ingested_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_id, as_of)
);

CREATE INDEX IF NOT EXISTS metric_history_metric_id_as_of_desc_idx
    ON public.metric_history (metric_id, as_of DESC);

CREATE INDEX IF NOT EXISTS metric_history_as_of_idx
    ON public.metric_history (as_of);

ALTER TABLE public.metric_history ENABLE ROW LEVEL SECURITY;

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
