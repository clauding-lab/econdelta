# Aggregate resilience: cumulative-monotonicity guard + granular Opus reject + NBR prompt hardening

**Date:** 2026-05-31
**Status:** Approved (design)
**Branch:** `feat/nbr-guard-granular-reject` (off `main` @ `202305a`)

## Background (the incident)

On 2026-05-31 the daily `aggregate` stage failed (exit 1) every run from 13:00 BDT. Root cause, confirmed from the box:

- `nbr_fytd_collected_cr` is aliased from the `tax_revenue` source (`aggregate_latest.py:397`) — a Bangladesh Bank PDF (publictn 5/27, "Component 9"), parsed deterministic-first (`pdf_component`) with an **Opus 4.8 LLM hybrid fallback** (`parsers/hybrid.py` → `run_max` default `claude-opus-4-8`).
- The **deterministic parse fails every run** ("Component 9 not found in PDF") → the LLM is the *sole* extractor.
- The value was **stable at 287,862.59 for 7 runs (May 24–30) on Opus 4.6**, then flipped to **33,522.0 on May 31** — the first parse after Opus 4.8 went live (#41). **Same PDF both days** (last fetch May 25); confounders (PDF change, LLM jitter) ruled out. → **Opus 4.8 mis-reads the PDF where 4.6 read it correctly.**
- A cumulative fiscal-year-to-date figure can't fall ~88% mid-year. The aggregate's Opus-4.8 *review* correctly flagged it and `return 1` (`aggregate_latest.py:654`), which (being all-or-nothing) froze the **entire** `latest.json` at 2026-05-30 for all consumers.

**Decision (user):** keep Opus 4.8 (don't revert the parser model); fix via prompt hardening + a guard, and make the reject granular so one bad field can't freeze everything.

## Goals

Three layered fixes (defense in depth), plus noted follow-ups.

### 1 · Granular Opus reject (aggregate layer)

When `review_data` returns `status == "reject"` (`aggregate_latest.py` ~L640–654), instead of an unconditional `return 1`:

- Collect flagged metric_ids = `verdict["anomalies"][].indicator` ∪ `verdict["missing"]`.
- **Hard-reject (today's behavior: keep yesterday's `latest.json`, `return 1`) if either:** any flagged id is not a key in `data` (verdict we can't act on), **or** flagged count **> K (K = 5)** (snapshot too broadly broken to publish).
- **Otherwise quarantine + publish:** for each flagged id, stale-fallback to its most-recent good value from the already-loaded 5-day `history` (list of archived `.data` dicts — keyed by the same flattened/aliased ids the verdict uses, so no source-id mismatch) and mark it stale; drop the id if no good value exists in `history`. Rebuild the bundle from cleaned `data`, then `write_latest` + archive + Supabase upsert → exit 0. *(Note: Fix 2's guard uses `_load_last_good_snapshot`, which is keyed by source id and runs pre-flatten — a different layer; Fix 1 must use `history` because it runs post-flatten on aliased ids.)*
- **Alert:** soften the Discord notify from "rejected — kept yesterday's" to "published with N fields quarantined: [ids]" (warn).
- **Invariant:** if quarantine can't proceed confidently, fall back to *exactly* today's hard-reject behavior — never regress the safety net.

### 2 · Cumulative-monotonicity guard (parse-quality, in aggregate)

Catch impossible drops in cumulative figures *before* they reach the Opus review.

- Add `"cumulative": true` to the FYTD/cumulative indicators in `config/sources-v3.json` (`tax_revenue`, the `nbr_*_collected_cr` components, the FYTD fiscal indicators).
- In aggregate's per-indicator processing: for a `cumulative` indicator, compare today's snapshot value against `_load_last_good_snapshot(id)`. If today is **> 5% below** the last-good value **within the same fiscal year**, treat today as bad → stale-fallback (reuse the existing path) + mark stale.
- **FY-aware:** Bangladesh fiscal year = **July–June**. A drop is permitted only when the last-good snapshot is in the *prior* FY and today is in the *new* FY (the legitimate July reset). Same-FY drops beyond tolerance are rejected.
- **Tunables (constants):** `CUMULATIVE_DROP_TOLERANCE = 0.05`; FY start month = July.

### 3 · NBR extraction prompt hardening (keep Opus 4.8)

- Rewrite `tax_revenue`'s config `task` from "Go to latest available PDF, Component 9" to explicitly demand the **cumulative July-to-current-month running total** in Component 9 — a large number (typically 200,000+ crore mid-year) — and **never a single-month figure**.
- Add a general rule to `claude_max/prompts/pdf_component.txt`: if the instruction names a fiscal-year-to-date / cumulative figure, return the running cumulative total, never a single-period value.
- **Validation (load-bearing, per the accepted risk):** run Opus 4.8 with the hardened prompt against the **saved 24-May PDF** and confirm it returns ~287,862 (not 33,522). If it still mis-reads, the guard (Fix 2) keeps NBR safely stale at last-good and we revisit the model decision.

### 4 · Follow-ups (noted, out of scope here)

- Fix the deterministic `pdf_component` Component-9 finder so `tax_revenue` doesn't depend on the LLM at all (best long-term).
- Fix the stale "deterministic + Sonnet hybrid" description in `deploy/econdelta-parse.service` (the hybrid uses Opus 4.8).

## Components touched

| File | Change |
|---|---|
| `aggregate_latest.py` | granular-reject branch; cumulative-monotonicity guard in per-indicator processing |
| `config/sources-v3.json` | `"cumulative": true` on FYTD indicators; hardened `tax_revenue` task instruction |
| `claude_max/prompts/pdf_component.txt` | general FYTD/cumulative extraction rule |
| `tests/test_aggregate_*.py` (+ new) | guard + granular-reject unit tests |

## Testing (TDD)

- **Guard:** cumulative indicator drops >5% within FY → stale-fallback; July-reset drop → allowed; non-cumulative indicator with a big drop → untouched; missing last-good → drop.
- **Granular reject:** reject verdict with ≤5 mappable anomalies → those fields stale-fallback'd, rest publish, exit 0; unmappable id → hard reject; count > 5 → hard reject; the NBR scenario end-to-end (FYTD flagged → quarantined, FX/reserves publish fresh).
- **Prompt:** Opus 4.8 + hardened prompt vs the saved 24-May PDF → ~287,862.
- Existing aggregate/opus_review unit tests stay green (they mock the `claude` subprocess).

## Out of scope

The deterministic Component-9 fix; the `parse.service` label fix; reverting the 4.8 bump; any change to the daily timers/cadence.
