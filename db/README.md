# db/ — Supabase schema

EconDelta's data layer. The `metric_history` table is the canonical
warm history; `data/archive/<date>.json` on ExonVPS is the cold backup.

## Files

| File | Purpose |
|------|---------|
| `schema.sql` | Current production DDL — what the table *should* look like right now |
| `migrations/0001_metric_history.sql` | Initial migration. Idempotent — safe to re-apply |
| `migrations/000N_*.sql` | Each future schema change goes here as a new numbered file |

## Applying

The table already lives in the shared brief Supabase project. New
infrastructure (a fresh dedicated EconDelta project, a staging mirror,
a local dev database) applies the schema like:

```bash
# Option A — psql with a connection string
psql "$DATABASE_URL" -f db/schema.sql

# Option B — supabase CLI (preferred when working from a Supabase project)
supabase db push
```

## Schema evolution

1. Add a new file `db/migrations/000N_<short-description>.sql`.
2. Mirror the same change into `schema.sql` so the canonical view is
   always current.
3. Apply via the same psql / `supabase db push` pattern.
4. Commit both files in the same PR — never let `schema.sql` drift from
   the latest migration.

**Compatibility rule**: once an indicator_id is in production, do not
rename it or change its unit silently. Add a new metric_id, update the
brief / consumers to read both during the transition, then deprecate
the old one with a comment in `sources-v3.json`. See
`docs/data-contract.md` for the full versioning policy.

## Authentication

- **Service role** — full read+write. Used by EconDelta's
  `utils/supabase_writer.py` and (transitionally) by the brief's
  `bb.py` / `dse.py` builders.
- **Anon key** — currently no policies grant anon access; reads via
  anon return zero rows. Future low-trust consumers (web apps, public
  dashboards) get a scoped role + RLS policy.

The schema enables RLS as a belt-and-braces measure even though the
service role bypasses RLS by default — keeps anon out by default if a
future operator adds an anon policy somewhere else.
