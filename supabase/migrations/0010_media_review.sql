-- 0010_media_review.sql — queue + decision record for the daily media screen.
-- Candidates land here as 'pending'; Copotron (Phase 3) flips status; the
-- aggregate (Phase 2) consumes 'approved' rows. Phase 1 only inserts 'pending'.
CREATE TABLE IF NOT EXISTS public.media_review (
    id             bigserial    PRIMARY KEY,
    detected_at    timestamptz  NOT NULL DEFAULT now(),
    metric_id      text         NOT NULL,
    parsed_value   numeric,
    parsed_as_of   date,
    press_value    numeric      NOT NULL,
    press_as_of    date         NOT NULL,
    kind           text         NOT NULL CHECK (kind IN ('fresher_period','same_period_conflict')),
    source_outlet  text,
    source_url     text         NOT NULL,
    source_quote   text,
    confidence     text,
    status         text         NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','approved','rejected','applied','superseded')),
    decided_at     timestamptz,
    decided_by     text,
    applied_at     timestamptz
);

CREATE INDEX IF NOT EXISTS media_review_status_idx ON public.media_review (status);
CREATE INDEX IF NOT EXISTS media_review_metric_idx ON public.media_review (metric_id, press_as_of);

ALTER TABLE public.media_review ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
    WHERE schemaname='public' AND tablename='media_review' AND policyname='service_role_all') THEN
    CREATE POLICY service_role_all ON public.media_review
      FOR ALL TO service_role USING (true) WITH CHECK (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
    WHERE schemaname='public' AND tablename='media_review' AND policyname='anon_read') THEN
    CREATE POLICY anon_read ON public.media_review FOR SELECT TO anon USING (true);
  END IF;
END $$;
