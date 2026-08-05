# BB NPL Structure Tracking — Design

**Date:** 2026-08-03
**Status:** Approved by owner (session 2026-08-03); AMENDED same day after the verification gate — see the Amendment section at the end. Where the amendment conflicts with earlier text, the amendment governs.
**Origin:** Owner deck "Small Loans Big Numbers — Bangladesh NPL Briefing" (BB data via Prothom Alo, 1 Aug 2026, position end-March 2026). Goal: EconDelta gains durable capacity to track the deck's data families as first-class metrics.

---

## Owner decisions (locked during brainstorming)

1. **End state:** data capacity first. New metrics in Supabase with a recurring capture path; deck-style analysis products can hang off this later. Not in scope now: a generated NPL briefing artifact.
2. **Source path:** BB primary publications only (QFSAR quarterly, FSR annual fallback). Accepted gap: band-wise defaulter *counts* (the deck's 45.43-lakh headline) are CIB-derived disclosures that BB does not publish on a schedule — NOT tracked. No media-screen extension in this build.
3. **Scope:** three families — band-wise NPL rates (+ outstandings where published), sectoral NPL split, CMSME segment rates. Bank-level detail (Krishi etc.) excluded.
4. **Freshness posture:** non-gating for the Monday briefing, sentinel-watched at honest quarterly windows. (Context: `gross_npl_ratio` alone kept the briefing dark for 4 weeks in July; ~21 new quarterly metrics must never gate publication.)
5. **Seed:** one-time supervised seed of the deck's reported primitives at `as_of 2026-03-31`, provenance `bb_via_press_static` (precedent: `mof_mfr_static`). Owner sign-off + before/after SELECT proofs required at execution time.

## Architecture decision

**Approach B — one dedicated document extractor** (chosen over per-indicator config entries and over a manual runbook):

- New module `scrapers/bb_npl_structure.py`, run_logs source `bb_npl_structure`, same dedicated-scraper shape as `fiscal_gdp_ratios.py`.
- Flow: resolve latest QFSAR PDF from BB's publication listing (runs on the ExonVPS box — BD IP gets through BB's F5 wall) → **one** LLM extraction call returning a strict JSON schema covering every metric → hard arithmetic self-check gate → upsert all rows via `upsert_metric_history`.
- Rationale: the QFSAR is one document carrying all three families. One controller-owned fetch + one schema-validated extraction with cross-field checks beats ~21 independent LLM parses of the same PDF (engineering discipline rule 7; Opus-4.8 mis-extract lesson — guards, not trust).
- Quarterly systemd timer on the box, alongside the existing timers.

## Metric inventory (~21 ids, final list pinned by the verification gate)

| Family | Ids (pattern) | Unit | Count |
|---|---|---|---|
| Band NPL rates | `npl_rate_band_lt1cr`, `_1_10cr`, `_10_20cr`, `_20_30cr`, `_30_40cr`, `_40_50cr`, `_gt50cr` | percent | 7 |
| Band outstandings | `loans_outstanding_band_lt1cr`, `_1_10cr`, `_gt50cr` | Tk crore | 3 |
| Sector lending shares | `lending_share_trade`, `_consumer`, `_construction`, `_agri` | percent | 4 |
| Sector NPL rates | `npl_rate_consumer` (+ others QFSAR permitting) | percent | 1–4 |
| CMSME segments | `npl_rate_cmsme_overall`, `_cmsme_cottage`, `_cmsme_medium`, `npl_rate_industry` | percent | 4 |
| Sector total | `total_bank_advances` | Tk crore | 1 |

- Flat `metric_history` rows (`metric_id`, `as_of`, `value`) — no new tables.
- Derived figures (implied NPL stock, implied impaired value, average exposure) are never stored; they are downstream arithmetic.
- Defaulter counts: excluded (owner decision 2).

## Self-check gate (load-bearing)

Nothing writes unless the extraction reconciles internally:

- The extraction schema includes the document's own overall NPL ratio. Band NPL rates weighted by band outstandings ≈ that **same-document** overall ratio within a stated tolerance — never the DB's `gross_npl_ratio`, which can legitimately be a vintage behind the document being extracted and would false-fail every fresh capture. Tolerance must absorb the unreported-band gap (only 3 of 7 outstandings are published); exact tolerance set during implementation against the real fixture.
- Sector lending shares sum ≤ 100; trade & commerce is the largest share.
- Every rate ∈ [0, 60]; every outstanding within a sane Tk-crore range.
- Extracted position-date must parse as a quarter-end date.
- Failure → **zero rows written** (all-or-nothing per document; no partial writes — the ratchet/partial-corruption class is structurally excluded), granular reject detail to Discord (NBR-guard precedent), run_logs `fail`.

## source_as_of

Content-derived from the PDF's own "position as at" text — never the run date, never the URL. Latest-idiom-match (gov PDFs print stale comparison dates; take the latest match), per the `pdf_table_row` landmines.

## Freshness / definitions

- `metric_definitions` seeded cadence=quarterly, grace calibrated to BB's **observed** QFSAR publication lag (~120–150 days from position date — measured during implementation against actual issue history, not guessed).
- Seeding is FIRST-INSERT-WINS (landmine): values must be correct on first insert; config edits never propagate later.
- Briefing: excluded from the core gate set (non-gating). Sentinel: watched at the honest windows above.

## Verification gate — step 0 of implementation, before any code

From the box or Hetzner: pull the current QFSAR and the latest FSR; confirm which of the three families each actually publishes; capture a real PDF fixture for tests. Any family missing from both publications → back to owner (drop, or downgrade to FSR-annual cadence). **No metric id ships unverified.** This Mac cannot do this step (BB F5 wall — do not re-attack).

## Static seed

- One supervised run of a new seeder (`seed_npl_structure.py` or equivalent) writing the deck's reported primitives at `as_of 2026-03-31`.
- Seed set: 7 band NPL rates; 3 published band outstandings (4.10 / 3.61 / 5.76 lakh crore); sector shares trade 32 / consumer 9 / construction 7; `npl_rate_consumer` 7; CMSME 34 / cottage 53 / medium 38; `npl_rate_industry` 32; `total_bank_advances` 17.84 lakh crore (= 17,84,000 crore).
- Excluded from seed: "not reported" outstandings, the vague "just over 4%" agriculture share, all derived figures, Mar-2025 values (counts only — out of scope).
- Provenance: `bb_via_press_static`. Execution requires owner sign-off + before/after SELECT proofs (house DB rules).

## Error handling

- Fetch failure → run_logs `fail` + Discord notify.
- Schema-invalid extraction → one retry, then FATAL notify, no write.
- Self-check failure → no write, granular reject (which check, expected vs got).
- Silent-failure ban: every abort path logs and notifies; no bare excepts.

## Testing (TDD throughout)

- Sabotage-proven gate discrimination: a deliberately mis-extracted fixture MUST be rejected (and the test must fail if the gate is deleted).
- Schema validation round-trip on the real fixture.
- `source_as_of` content-derivation against the real fixture (including stale-comparison-date discrimination).
- Seeder idempotency (re-run writes nothing new).
- Sentinel window boundary tests (fresh at lag−1d, stale at lag+1d).
- Registry/coverage test so the new source can't silently drop out of the run schedule (corridor-parser-registry lesson).
- Fixtures mirror the real producer's PDF output, not idealized tables (engineering discipline rule 2).

## Out of scope (flagged, not built)

- `gross_npl_ratio` is one vintage stale vs the press (DB 32.26% vs Mar-2026 32.7% in the deck). That is the existing media-screen override path's job.
- Defaulter counts by band (no scheduled BB source; would need media-screen extension — a future decision).
- Any generated NPL-briefing analysis product.
- The Brief rendering of any of this (separate repo).

## Deliverable shape

Likely 2 PRs: (1) extractor + definitions + sentinel wiring + tests; (2) seeder + supervised seed execution. Final split decided in the implementation plan. Gate for every PR: `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check .` run bare, exit 0.

---

## Amendment — 2026-08-03, post-verification (owner-approved; governs over earlier text)

The verification gate ran against the real documents (QFSAR Jul–Sep-2025 issue from the box; FSR 2025 fetched live from BB via the box). Findings:

1. **QFSAR carries NONE of the three families** (its NPL coverage: overall ratio, bank-cluster, concentration, category composition). It is also STALLED — the Jul–Sep-2025 issue is still the newest as of Aug 2026 (~10-month lag, identical sha across 4 monthly fetches).
2. **FSR 2025 (annual, published ~Jun 2026, position end-Dec-2025) DOES publish the sectoral family** — Table 2.3 "Sector-wise Non-performing Loans Distribution": 8 top-level sectors + sub-sectors, each with outstanding, gross NPL stock, NPL ratio, share of loans, share of NPLs. Richer than the deck's 4-sector press cut. Full shares column → a complete-reconciliation gate (weighted sector rates ≈ printed overall ratio, shares ≈ 100, stock/advances ≈ ratio).
3. **Band-wise NPL and CMSME segment rates appear in NEITHER publication** — they are press/parliament disclosures (CIB-derived).

**Owner decisions (2026-08-03, second batch):**

- **Rebuild shape: "FSR sectoral + seed".** The extractor targets FSR Table 2.3 (annual). The scraper performs its own FSR discovery+fetch on the box (proven live: `_download_index_html` + `discover_latest_pdf` + `fetch_pdf`), artifact dir `data/_pdfs/bb_npl_structure/`. The QFSAR-artifact-reuse design is dead.
- **Band + CMSME families become SEED-ONLY series** (static Mar-2026 press values, provenance `bb_via_press_static`). No automated writer exists for them until a future press/media-screen decision.
- **Sentinel posture: `accepted_stale`** for all these ids (structural source lag exceeds every sentinel window; precedent `tax_gdp_ratio`/`rev_gdp_ratio`). Non-gating for the briefing stands. Capture failures still surface via run_logs + the scraper's own Discord notifies.
- **Sector taxonomy = the FSR's**, not the deck's press cut. Consequence for the seed: the deck's 4-sector shares (trade 32 / consumer 9 / construction 7), `npl_rate_consumer` 7.0, and `npl_rate_industry` 32.0 are DROPPED from the seed — they are a different taxonomy from the ongoing FSR series and would create orphan/confusable series. Seed = 7 band rates + 3 band outstandings + 3 CMSME rates (overall 34 / cottage 53 / medium 38) + `total_bank_advances` 1,784,000 crore = 14 values at as_of 2026-03-31.

**Amended metric inventory (35 ids):**

- FSR sector NPL rates (8): `npl_rate_sector_agriculture`, `_industrial_mfg`, `_industrial_services`, `_consumer_credit`, `_trade_commerce`, `_nbfi`, `_capital_market`, `_other`
- FSR sector lending shares (8): `lending_share_sector_<same 8 suffixes>`
- FSR sub-sector NPL rates (4, write-if-present): `npl_rate_sub_rmg`, `npl_rate_sub_construction`, `npl_rate_sub_housing_finance`, `npl_rate_sub_smc_industries` (Small/Medium/Cottage industries row)
- FSR totals (2): `total_bank_advances`, `gross_npl_stock` (both Tk crore; FSR prints billion BDT — code converts ×100; the LLM extracts VERBATIM billions, never converts)
- Seed-only (13): `npl_rate_band_lt1cr`…`_gt50cr` (7), `loans_outstanding_band_lt1cr`/`_1_10cr`/`_gt50cr` (3), `npl_rate_cmsme_overall`/`_cottage`/`_medium` (3)
- `overall_npl_ratio_fsr` is extracted as a CHECK field only — never stored (`gross_npl_ratio` remains the QFSAR-sourced overall series; the FSR's 30.60 is a different vintage of the same concept and must not collide).

**Amended freshness:** definitions cadence `fiscal_year`, grace 400d (truth-in-labeling for the DB view); sentinel `_SCRAPER_CADENCE` entries `fiscal_year` + ALL 35 ids in `ACCEPTED_STALE_METRIC_IDS`.

**Fixture:** `tests/_pdfs/fsr_fixture.pdf` (FSR 2025, 6.1MB, sha verified against box fetch) + `tests/fixtures/fsr_fixture_text.txt` (397,499 chars). The QFSAR fixture was discarded (wrong document for the amended build).

**Also observed, out of scope:** the DB's `gross_npl_ratio` 32.26% is the stale Sep-2025 QFSAR figure; press reports 32.7% for Mar-2026. Existing media-screen override path's territory.
