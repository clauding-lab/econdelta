-- 0011_drop_media_review_anon_read.sql — remove public read access to the
-- internal media-review queue.
--
-- 0010 added `anon_read` (FOR SELECT TO anon USING (true)), which let the PUBLIC
-- anon key read every media_review row — source quotes, outlets, decided_by — via
-- PostgREST. No PWA surface consumes media_review, so this exposed internal
-- editorial state with no upside. The backend pipeline uses `service_role_all`,
-- which is unaffected. Re-add a column-scoped policy only if a read-only display
-- use case ever needs it.
DROP POLICY IF EXISTS anon_read ON public.media_review;
