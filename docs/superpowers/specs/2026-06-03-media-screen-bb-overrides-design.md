# Design — Daily Media Screen for BB Data (human-gated overrides)

**Date:** 2026-06-03 (BDT)
**Status:** Approved design — pending implementation plan
**Owner:** Adnan (vibe-coder; directs agents, does not hand-write code)
**Repo:** `econdelta` (`~/Projects/clauding-lab/econdelta`) — runs on ExonVPS Dhaka; writes the shared Supabase `metric_history` consumed by The Brief.

---

## 1. Problem & purpose

BB publishes some figures (e.g. the QFSAR's NPL/CAR ratios) on a long lag, while the
press reports fresher numbers for the same indicators well before BB's PDFs catch up.
The 2026-06-03 NPL incident is the canonical case: The Brief showed the QFSAR's
**35.73% (end-Sep-2025)** while the press already reported **32.26% (end-Mar-2026)** —
because EconDelta only ingests the lagging QFSAR, not the faster press release.

This feature adds a **daily media screen** (a Claude agent on the ExonVPS) that compares
press-reported numbers for BB indicators against the currently-parsed values, surfaces
**material, period-pinned mismatches** to Adnan via Discord for **human approval**, and —
only on approval — applies the press value to `metric_history` as a temporary bridge until
BB's own pipeline publishes that period.

**This is the human-gated, general form of the "faster source" follow-up** noted after the
NPL fix (PRs #64/#65). It is explicitly NOT an automatic press-ingestion path — the prior
automatic press cross-checkers were retired for flapping (see §11).

## 2. Decisions (locked during brainstorming)

| # | Decision | Choice |
|---|---|---|
| D1 | Detection intent | **Both**: fresher-period figures AND same-period conflicts |
| D2 | Metric scope | **All** BB-extracted metrics (candidate set); metrics the press doesn't mention produce no candidate |
| D3 | Approval UX | **Reply to Copotron** in Discord (`approve N` / `reject N`) |
| D4 | Noise control | **Strict**: only ping when the press period is explicit AND the value differs beyond a per-metric rounding tolerance |
| D5 | Write architecture | **A** — single `media_review` table; Copotron only flips status; EconDelta `aggregate` is the sole writer to `metric_history` |
| D6 | Supersede rule | **Any later BB parse, incl. same period** — BB-official reclaims once it has genuinely fresher data; the override is a temporary bridge, not permanent |

## 3. Architecture (Approach A — EconDelta stays the only `metric_history` writer)

```
 ExonVPS (BD egress)                Shared Supabase                 Hetzner
 ┌──────────────────┐         ┌───────────────────────┐        ┌──────────────┐
 │ media-screen job │──insert │  media_review (queue) │        │  Copotron    │
 │ (daily timer)    │ pending │  status: pending →    │◀──flip─│ (you reply   │
 │ extract + filter │────────▶│  approved / rejected  │ status │  approve/    │
 └────────┬─────────┘         └──────────┬────────────┘        │  reject)     │
          │ Discord digest               │ reads approved      └──────────────┘
          │ (utils/notifier.py)          ▼
          ▼                     EconDelta aggregate ──writes──▶ metric_history
   Adnan's Discord channel      (sole writer; applies +         (source='media-
                                 supersede logic)                approved:<outlet>')
```

The screen and the apply both live inside EconDelta, so the "single source of truth /
single writer" invariant (`utils/supabase_writer.py` docstring) is preserved. Copotron's
only mutation is flipping a status flag on a `media_review` row — it never writes
`metric_history`.

## 4. Components (each one purpose, interface, dependency)

- **`scrapers/media_screen.py`** (new) — daily entry point. Sweeps the configured outlets,
  extracts every number + its stated reporting period via the `claude` CLI, matches them to
  the BB metric catalog, compares to `get_latest`, applies the strict filter (§6), inserts
  surviving candidates into `media_review` (status=`pending`), and fires one Discord digest.
  Reuses: `fetchers/news_article_discovery.py`, the HTML fetcher, `claude_max/max_client.py`,
  `utils/notifier.py`. Run via a new `econdelta-media-screen.{service,timer}`.
- **`media_review` table** (new migration) — the queue + decision record (§5).
- **Apply step in `aggregate_latest.py`** (extend) — before/with the normal upsert, read
  active approved overrides and apply the supersede logic (§7).
- **Copotron command handler** (Hetzner) — parses `approve N` / `reject N` from the Discord
  channel and flips `media_review.status`; never touches `metric_history`.

## 5. Data model — one new table

```sql
CREATE TABLE public.media_review (
  id             bigserial PRIMARY KEY,
  detected_at    timestamptz NOT NULL DEFAULT now(),
  metric_id      text NOT NULL,          -- EconDelta indicator id; alias propagation (#65) carries it to brief keys
  parsed_value   numeric,                -- current get_latest() value at detection (the supersede baseline)
  parsed_as_of   date,                   -- current get_latest() period at detection
  press_value    numeric NOT NULL,       -- the number the screen extracted
  press_as_of    date NOT NULL,          -- the period the press cites (STRICT: required, never null)
  kind           text NOT NULL,          -- 'fresher_period' | 'same_period_conflict'
  source_outlet  text,                   -- e.g. 'thedailystar' / 'tbsnews'
  source_url     text NOT NULL,
  source_quote   text,                   -- the exact sentence, for Adnan's review
  confidence     text,                   -- screen's note / rationale
  status         text NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | applied | superseded
  decided_at     timestamptz,
  decided_by     text,                   -- 'discord:adnan'
  applied_at     timestamptz
);
```

RLS mirrors `metric_history`: service-role full access; anon read (so the screen and
Copotron operate with the service key, and the value is auditable). Migration applies via
the SQL editor (Adnan's login) per the project's migration mechanism — no programmatic DDL.

## 6. Detection + strict filter (the flap-killer)

"All metrics" (D2) is handled by **extract-then-match**, not search-per-metric: the screen
reads the day's relevant articles, extracts every `(number, reporting-period, indicator)`
triple the `claude` CLI can identify, then matches against the BB metric catalog. A
candidate is emitted only when (D4):

1. **The reporting period is explicit** in the source text. No period → discarded. (This is
   exactly the failure that retired the old NBR cross-checkers — see §11.)
2. **The value differs from the parsed value beyond a per-metric rounding tolerance**
   (e.g. ratios within ±0.05pp are "same"; tolerances configured per metric).
3. **Not a duplicate** of an already-`pending` or recently-`rejected` candidate (so the same
   article doesn't re-ping day after day).

`kind` is derived: `press_as_of > parsed_as_of` → `fresher_period`; `press_as_of == parsed_as_of`
with a material value diff → `same_period_conflict`.

## 7. Apply + supersede semantics (D6)

On approval, EconDelta `aggregate` (sole writer) applies the override:
`metric_history` row at **`as_of = press_as_of`** (the cited period — never the approval
date, which would re-introduce the false-freshness bug #64 fixed), `source = 'media-approved:<outlet>'`.

- **`fresher_period`** (the main case — press leads BB's lag): the override `as_of` is newer
  than anything the automated pipeline writes, so it becomes `get_latest` and persists with
  **no shielding needed** (the daily parse writes an older `as_of` and never collides). It
  **retires automatically** when BB's pipeline produces a value at that period or later —
  then BB-official wins via the normal `(metric_id, as_of)` upsert + `get_latest`.
- **`same_period_conflict`** (a correction to a period BB already has): the override must be
  held against the daily *re-emission* of the identical figure (not "fresher"), and retires
  the moment BB's parsed value for that period **changes** vs `parsed_value` (the recorded
  baseline) — a genuine BB revision. This baseline comparison is the only non-trivial bit;
  the exact release predicate is finalized with tests in the implementation plan.

Alias propagation (the `_build_source_as_of_map` mechanism shipped in #65) carries an
override keyed on the EconDelta indicator id (`gross_npl_ratio`) to the brief keys the SPA
reads (`banking_npl_pct`).

## 8. Daily flow (end to end)

1. **Screen** runs on ExonVPS in the evening BDT (after the press publishes for the day, so
   candidates are ready for morning review and approved changes flow into the next 06:30
   publish). 2. Extract + match + strict filter. 3. Insert `pending` rows + one Discord
   digest ping. 4. Adnan replies to Copotron `approve 7` / `reject 7` (can interrogate the
   candidate first). 5. Copotron flips `status` → `approved`/`rejected`. 6. Next `aggregate`
   run applies `approved` rows → `metric_history`, sets `applied`. 7. Next publish reflects it.

## 9. Failure modes (all fail safe)

- Screen errors (fetch/extract/LLM) → zero candidates emitted; never a bad write.
- Discord down → rows persist as `pending`; the digest retries next run.
- Aggregate apply failure → row stays `approved`, retried next aggregate run (idempotent on
  `(metric_id, as_of)`).
- Nothing auto-applies without Adnan's explicit flip. A rejected candidate leaves
  `metric_history` untouched.

## 10. Testing

- **Unit:** period-extraction; strict filter (tolerance + period-required + dedup); `kind`
  derivation; apply→`as_of` mapping (press period, not today); supersede release predicate
  for both `kind`s; alias propagation reuse.
- **Integration:** `pending` → `approved` → aggregate apply → `metric_history` row at the
  press period; `same_period_conflict` survives daily re-emission then yields on a value change.
- **`--dry-run`** screen that prints candidates without inserting (sign-off gate before the
  prod timer, per landmine 5 / the F4 dry-run pattern).

## 11. Prior art & landmines (read before building)

- **The old press cross-checkers FLAPPED.** `nbr_fytd_collected_tbs` / `_dailystar` were
  retired (2026-05-25) because tag-listing pages drifted onto articles covering different
  fiscal-year windows, so the cross-check flapped. **Mitigation here:** strict period-pinning
  (§6.1) + human approval gate. If a candidate's period can't be pinned, it is discarded, not
  raised.
- **BB sources sit behind a CAPTCHA/F5 wall** (landmine 24) — but this feature reads *press*
  sites (Daily Star, TBS, Bonik Barta, FE, Dhaka Tribune), not BB directly. Press sites are
  reachable from the ExonVPS (BD IP). Foreign-IP fetch of these may 403 (observed from the
  Mac), which is why the screen runs on the ExonVPS.
- **`url=` is the Supabase base-URL override, not provenance** (landmine 22) — provenance
  goes in the `source` column. Media overrides use `source='media-approved:<outlet>'`.
- **`as_of` must be the reporting period, not the run date** (the #64/#65 fix) — overrides
  follow the same rule, hence `as_of = press_as_of`.

## 12. Phased build (each its own PR, testable in isolation)

1. **Schema + screen + Discord ping** — `media_review` migration, `scrapers/media_screen.py`,
   the strict filter, the digest. Detection only; no apply. Safe to run/observe live.
2. **Apply path** — extend `aggregate_latest` to consume `approved` rows + the supersede logic.
3. **Copotron command** — the `approve N` / `reject N` handler that flips status.

## 13. Open items / future (out of scope for v1)

- Tuning per-metric tolerances after observing real candidate volume.
- Whether to expand beyond Bangladesh-economy press outlets.
- The ~9 other slow-cadence metrics whose *parsers* lack date recovery (surfaced by the
  guardrail in #64) — a separate parser fix, independent of this feature.

## 14. Cross-references

- PRs #64 (`source_as_of` recovery on the LLM path) and #65 (alias propagation) — the
  freshness/aliasing machinery this feature reuses.
- `utils/supabase_writer.py` — the single-writer invariant.
- `supabase/migrations/0001_metric_history.sql` — the target table schema.
- `AGENTS.md` landmines 5, 22, 24; the retired-cross-checker note in `BRIEF_ALIASES`.
