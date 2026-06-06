# Fiscal monthly backfill — per-fiscal-year budget anchors

**Date:** 2026-06-06
**Status:** Approved design (pre-implementation)
**Author:** Claude (directed by Adnan)
**Scope:** EconDelta only — `scripts/mfr_parser.py`, `scripts/backfill_fiscal.py`, tests. The Brief §Fiscal charts (F7b) remain a separate follow-on.

---

## Context

EconDelta backfills two **monthly** fiscal metrics from the MoF Monthly Fiscal Report (MFR) PDFs:

- `govt_bank_borrow_monthly_cr` — single-month Government borrowing from the banking system (Net), BDT crore (MFR Table 6, row "Borrowing from Banking System (Net)").
- `nbr_revenue_monthly_cr` — single-month NBR tax revenue, BDT crore (MFR Table 4, row "a. NBR").

(There is also `adp_completion_pct_annual`, an annual metric — already complete, **out of scope** here.)

### What prompted this

A request to "do F7" (deepen fiscal data for The Brief). Two findings reframed the work:

1. **Forward-fill is a no-op.** The MoF archive (fetched live, HTTP 200) lists 75 MFRs; the **newest is October 2025**, and Supabase already holds Jul–Oct 2025. There are **zero** reports newer than Oct 2025 — MoF simply hasn't published past it (~8-month lag). The monthly series is already current with the source.
2. **The only way to add depth is backward-fill.** The archive spans **2019-07 → 2025-10** (~6 fiscal years). But the parser hardcodes the **FY26** annual-budget anchor, so FY25/FY24/older reports cannot parse as-is.

### Why the parser is FY-specific

`mfr_parser` does not use fixed column positions (the MFR column layout shifts between July and Aug+, and between fiscal years). Instead it locates the **current fiscal year's annual *Budget* value** in the row (stable across all 12 issues of that FY), then reads the next two numbers as `single_month` and `fytd`. The current code hardcodes the FY26 budget (`104000` / `499001`) in `parse_one_mfr` (`backfill_fiscal.py:212-215`), so it only parses FY26 reports.

---

## Goal

Backfill `govt_bank_borrow_monthly_cr` and `nbr_revenue_monthly_cr` **as deep as the archive cleanly parses** (target: back to FY20, archive limit 2019-07), keeping only months that parse **and** pass the FYTD self-check. Drop (with logging) anything that fails. Get The Brief's fiscal charts the depth they need (today: 4 monthly points → target: ~24+).

---

## Evidence (empirical, 2026-06-06)

Downloaded representative PDFs per fiscal year and dumped the raw row tokens with the existing parser.

**Known-good check — Oct 2025 (FY26):** value-anchor reproduces the DB exactly.
- BORROW row: `137,500 | 99,000 | 16,715 | 15,651 | 114,161 | 104,000 | 5,720 | 7,570` → anchor `104,000` → **5,720 / 7,570** ✓ (DB has 5,720)
- NBR row: `480,000 | 463,500 | 27,289 | 101,442 | 368,715 | 21.1 | 79.6 | 499,001 | 28,027 | 117,420 | 23.5` → anchor `499,001` → **28,027 / 117,420** ✓ (DB has 28,027)

**Positional shortcut ("last two numbers") is rejected.** It works for BORROW but **breaks for NBR**, whose row carries trailing growth-% columns (e.g. `23.5`): positional`[-2,-1]` yields `(117,420, 23.5)` — wrong. The column *count* also shifts year-to-year (FY25 BORROW has a trailing extra column FY26 lacks). The value-anchor is the only robust method.

**Per-FY budget anchors are self-revealing and cross-validate** (each report prints its own year's budget AND the prior year's, so values recur across two years' reports):

| FY (end-year) | BORROW budget | NBR budget | Cross-check |
|---|---|---|---|
| 2026 | 104,000 | 499,001 | in code; reproduces DB |
| 2025 | 137,500 | 480,000 | appears in FY26 + FY25 reports |
| 2024 | 132,395 | 430,000 | appears in FY25 + FY24 reports |
| 2023 | 106,334 | 370,000 | from FY24 report's prior-year column |

(Anchors for FY22/FY21/FY20 to be sourced the same way during implementation, each documented + cross-checked; any FY whose anchor can't be confirmed is simply skipped.)

---

## Design

**Approach: per-fiscal-year budget anchors + self-check hard-gate.** (Rejected alternatives: *positional extraction* — NBR breaks it; *auto-bootstrapping anchors from each report's prior-year column* — works but adds a positional dependency + complexity for a one-time historical backfill; YAGNI.)

### 1. `mfr_parser.py` — unchanged in spirit
Keep the budget-anchor extraction (`_extract_month_fytd`, `parse_bank_borrowing`, `parse_nbr_revenue`). The `FiscalRow.fy26_budget` field is renamed to a neutral `fy_budget` (provenance only). No change to the extraction algorithm.

### 2. `backfill_fiscal.py` — per-FY anchor table
Replace the two `FY26_*_BUDGET_CRORE` constants with two dicts keyed by **FY-end year**:

```python
FY_BORROW_BUDGET = {2026: 104000.0, 2025: 137500.0, 2024: 132395.0, 2023: 106334.0, ...}
FY_NBR_BUDGET    = {2026: 499001.0, 2025: 480000.0, 2024: 430000.0, 2023: 370000.0, ...}
```
Each entry documented with provenance + cross-check note.

### 3. `parse_one_mfr` selects anchor by the report's own fiscal year
```python
def fiscal_year_of(year, month):   # FY-end year
    return year + 1 if month >= 7 else year
```
Look up `FY_BORROW_BUDGET[fy]` / `FY_NBR_BUDGET[fy]`. If `fy` is absent → raise/skip (logged), do not guess. (`parse_report_month` already yields `(year, month)`.)

### 4. Self-check becomes a hard gate for backfilled months
Today `self_check_fytd` only logs warnings; the row ships regardless. Change: a month is **written only if it parses AND does not FAIL the self-check** (single ≈ Δfytd, 5% tol).

- **Fails the check** (rel error > tolerance) → dropped + logged.
- **Cannot be checked** (July = FY first month; or no prior-month neighbor present) → **kept** on parse-strength alone (these are the anchor points). This matches "keep only what cleanly parses": we drop *failures*, not *uncheckable* months.

Gate applies to the backfill path. Existing FY26 rows already in the DB are untouched (re-confirmed, not rewritten).

### 5. Scope of the write
All cleanly-parsing months back to the archive limit (2019-07), both monthly metrics. ADP untouched.

---

## Data flow

```
MoF archive page (live HTML, server-rendered)
  └─ harvest office-mof *.pdf links + month labels  (one-off; map saved to links-file)
       └─ per report: download → parse_report_month → fiscal_year_of → anchor lookup
            └─ parse_bank_borrowing / parse_nbr_revenue (value-anchor)
                 └─ build series_by_month → self_check_fytd (HARD GATE) → drop failures
                      └─ --dry-run: print every month + self-check  (REVIEW GATE)
                           └─ VPS prod-run: upsert metric_history_monthly (idempotent)
```

Link discovery: the live MoF page server-renders all PDF links (no Firecrawl needed; Firecrawl was out of credits). Map links→month via the anchor **title labels**, not the URL path (path month is unreliable, e.g. `2026/0`).

---

## Safety properties

- **Fail-safe:** wrong/missing anchor → value not found in row → skip (not silent-bad-data). Mis-extraction → self-check failure → drop.
- **Idempotent:** upsert on `(metric_id, as_of)` with `resolution=merge-duplicates`; re-runs safe.
- **Non-destructive:** never overwrites existing FY26 rows except to re-confirm identical values.
- **Human review gate:** dry-run prints every parsed month + the full self-check for Adnan's review **before** any VPS write.
- **Prod write runs on the VPS** (service-role key in `/etc/econdelta.env`); the harness blocks prod writes from the Mac. Dry-run + parse happen on the Mac (BD-side egress).

---

## Testing (TDD)

- Unit: real PDF-derived token fixtures per FY (FY26/FY25/FY24) → assert correct `single`/`fytd` for both metrics.
- Unit: `fiscal_year_of` boundary (June→FY same year, July→FY+1).
- Unit: anchor lookup miss → skip/raise (not a silent FY26 fallback).
- Unit: self-check gate — a deliberately-broken consecutive pair gets the offending month **dropped**; an uncheckable (July / isolated) month is **kept**.
- Regression: existing FY26 extraction + the registry-coverage / existing fiscal tests stay green.

---

## Non-goals

- The Brief §Fiscal charts (F7b) — separate follow-on.
- ADP annual metric — already complete.
- Auto-discovery/auto-ingest of *future* MFRs — separate (MoF hasn't published past Oct 2025 anyway).
- Restating/normalizing for mid-year budget revisions — the anchor uses the stable original *Budget* column, not *Revised*.

---

## Rollout

1. TDD the parser/orchestrator change (this branch: `feat/fiscal-backfill-per-fy-anchor`).
2. Local dry-run over the full archive → review parsed months + self-check with Adnan. Source any remaining FY anchors (FY22↓) here.
3. On approval → VPS prod-run (idempotent upsert).
4. Verify via anon REST: new months present, sane values, correct `source_as_of` dating.
5. PR per repo style (EconDelta uses merge-commit per recent history).

---

## Open questions / risks

- **Older layout drift (FY22↓):** pre-FY23 MFRs may use a different table format; "as deep as it cleanly parses" + self-check handles this (non-conforming reports drop out). We accept a natural depth floor wherever clean parsing stops.
- **Anchor sourcing for FY22↓:** to be read off real PDFs during implementation and cross-checked; unconfirmed years are skipped, not guessed.
- **Uncheckable months kept:** July and gap-isolated months ride on parse-strength (documented tradeoff). Contiguous backfill minimizes gaps.
