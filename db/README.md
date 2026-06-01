# db/ — Supabase schema reference

EconDelta's data layer. The `metric_history` table is the canonical
warm history; `data/archive/<date>.json` on ExonVPS is the cold backup.

## Files

| File | Purpose |
|------|---------|
| `schema.sql` | Current production DDL — what the tables *should* look like right now. A canonical **reference snapshot**; not applied directly in normal operation. |

> **Migrations moved.** The numbered migration files now live in
> [`supabase/migrations/`](../supabase/migrations/) and are managed by the
> Supabase CLI — they are no longer hand-applied in the dashboard SQL editor,
> and `db/migrations/` no longer exists. See "Schema evolution" below.

## Applying migrations

This project **shares its Supabase database with The Brief**, so `supabase db push`
**does not work** — push requires this repo to hold the database's *entire*
migration history, but The Brief's migrations live elsewhere, so push aborts with
"remote migration versions not found in local migrations directory."

Instead, apply a migration file directly (Docker-free, idempotent-safe) from a
linked Mac checkout:

```bash
supabase link --project-ref <ref>                            # one-time, per checkout
supabase db query --linked -f supabase/migrations/<file>.sql # applies that file
```

Migrations are idempotent (`create table if not exists`, `if not exists` policy
guards), so re-applying is harmless. **The migration files in git are the source
of truth** for what exists — the DB's `schema_migrations` table is a mixed,
multi-app log and is not authoritative here.

## Schema evolution

1. `supabase migration new <short_description>` — creates a new file under
   `supabase/migrations/`.
2. Write the change. Keep it **idempotent** (`create table if not exists`,
   `if not exists` policy guards) so a re-apply is harmless.
3. Mirror the same change into `schema.sql` so the canonical view stays
   current — never let `schema.sql` drift from the latest migration.
4. Commit both in the same PR, then apply with
   `supabase db query --linked -f supabase/migrations/<file>.sql` (NOT `db push`
   — it won't work on this shared DB; see "Applying migrations" above).

**Compatibility rule**: once an indicator_id is in production, do not
rename it or change its unit silently. Add a new metric_id, update the
brief / consumers to read both during the transition, then deprecate
the old one with a comment in `sources-v3.json`. See
`docs/data-contract.md` for the full versioning policy.

## Authentication & RLS

- **Service role** — full read+write. Used by EconDelta's
  `utils/supabase_writer.py` (the sole writer).
- **Anon key** — scoped **read-only**. `metric_history`, the monthly
  history/catalog tables, and the auction tables grant `anon` SELECT
  (migrations 0005 / 0006 / 0007 / 0009) so the PWA, The Brief, and
  YieldScope can read them. Anon cannot write.

RLS is enabled on every table. The service role bypasses RLS by design;
anon is limited to the explicit SELECT policies above.
