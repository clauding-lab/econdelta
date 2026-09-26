# Reviewed-candidate history repair — 26 September 2026 BDT

**Status: proposed exact operations; production approval and application outstanding.** Code and isolated rehearsal are separate from authorization to alter live history.

Candidate SHA-256: `f8f8fcf685e5394a80d2629210229a8307d1240b9688ed45d1612c338dea5e56`. EconDelta code `a368600`; Brief exporter `3a543df`. Private execution package is retained in the cross-project repair workspace, not committed. The manifest contains the full 40-character commits, source hashes, complete before/after images, exact keys and dependency order.

The proposal has **1,596 operations: 6 inserts, 55 updates and 1,535 exact-key removals**. Four tables change. Eight other backed-up tables, including every published edition and its children, are unchanged. The initial 12-table backup has 92,109 rows; every file hash and unique key was checked and its actual PostgreSQL restore previously passed. It is nontransactional; R3 must take and reconcile a final paused-writer snapshot.

## What the removals mean

The 1,535 removals are **proposed exclusions from active history, not a claim that all 1,535 numbers are false**. Many legacy daily stamps do not carry recoverable source periods. Some could represent legitimate observations; this proposal deliberately retains only independently verified periods within the selected families. Approval therefore accepts reduced historical chart coverage. All removed rows remain unchanged in the hashed original export, and reverse operations restore their exact original images. There is no broad SQL predicate or whole-table delete.

## Counts by exact series

| Table / series | Insert | Update | Remove |
|---|---:|---:|---:|
| `metric_definitions` / `gdp` | 0 | 1 | 0 |
| `metric_definitions` / `gdp_growth_fy_pct` | 1 | 0 | 0 |
| `metric_definitions_monthly` / `net_reserves_bpm6_usd_bn_monthly` | 0 | 1 | 0 |
| `metric_history` / `bank_borrowing_for_deficit_financing` | 0 | 2 | 35 |
| `metric_history` / `banking_broad_money` | 0 | 3 | 137 |
| `metric_history` / `banking_deposits` | 0 | 2 | 36 |
| `metric_history` / `banking_excess_liquid` | 0 | 0 | 38 |
| `metric_history` / `banking_money_multiplier` | 0 | 2 | 36 |
| `metric_history` / `banking_reserve_money` | 0 | 3 | 137 |
| `metric_history` / `broad_money` | 0 | 3 | 137 |
| `metric_history` / `crr_utilisation_pct` | 1 | 1 | 109 |
| `metric_history` / `currency_outside_bank` | 0 | 2 | 36 |
| `metric_history` / `debt_gdp_ratio` | 0 | 0 | 29 |
| `metric_history` / `debt_gdp_ratio_proj` | 0 | 0 | 6 |
| `metric_history` / `deposits_held_with_bb_crr` | 0 | 2 | 36 |
| `metric_history` / `deposits_of_the_system` | 0 | 2 | 36 |
| `metric_history` / `domestic_borrowing_for_budget_deficit` | 0 | 2 | 35 |
| `metric_history` / `excess_liquid_asset_total_minimum` | 0 | 0 | 38 |
| `metric_history` / `fiscal_bank_borrow_trn` | 0 | 2 | 35 |
| `metric_history` / `fiscal_foreign_borrow_trn` | 0 | 2 | 35 |
| `metric_history` / `fiscal_govt_borrow_trn` | 0 | 2 | 35 |
| `metric_history` / `foreign_borrowing_for_budget_deficit` | 0 | 2 | 35 |
| `metric_history` / `gdp` | 0 | 0 | 126 |
| `metric_history` / `gdp_growth_fy_pct` | 2 | 0 | 0 |
| `metric_history` / `money_multiplier` | 0 | 2 | 36 |
| `metric_history` / `monthly_import_lc_opening` | 1 | 3 | 35 |
| `metric_history` / `monthly_import_lc_settlement` | 1 | 3 | 35 |
| `metric_history` / `non_bank_borrowing_for_deficit_financing` | 0 | 2 | 35 |
| `metric_history` / `reserve_money` | 0 | 3 | 137 |
| `metric_history` / `slr_utilisation_pct` | 0 | 0 | 110 |
| `metric_history_monthly` / `tbill_182d_yield_monthly` | 0 | 1 | 0 |
| `metric_history_monthly` / `tbill_364d_yield_monthly` | 0 | 1 | 0 |
| `metric_history_monthly` / `tbill_91d_yield_monthly` | 0 | 1 | 0 |
| `metric_history_monthly` / `yield_10y_monthly` | 0 | 1 | 0 |
| `metric_history_monthly` / `yield_15y_monthly` | 0 | 1 | 0 |
| `metric_history_monthly` / `yield_20y_monthly` | 0 | 1 | 0 |
| `metric_history_monthly` / `yield_2y_monthly` | 0 | 1 | 0 |
| `metric_history_monthly` / `yield_5y_monthly` | 0 | 1 | 0 |

## Source-supported corrections and unresolved decisions

- **MEI banking:** June-MEI May readings are preserved at 31 May before occupied 30 June keys are replaced with July-MEI June readings. Deposits: 2,041,692.7 → 2,079,911.1 crore; currency outside banks: 349,374 → 336,375.2 crore; reserve money: 485,542.3 → 476,368.6 crore; deposits held with BB: 115,326.7 → 106,100.1 crore; multiplier: 4.92 → 5.07. The source held-with-BB row includes NBFCs; the ratio is not statutory compliance.
- **WSEI money:** broad money and reserve money are 2,422,923.9 and 463,461.4 crore for 31 July. The July headings do not carry a P/R marker: these facts, their aliases and both July LC facts carry release status `unknown`. Their September daily stamps are excluded. Genuine May/June MEI observations remain separately dated; no cover date is used as a period.
- **LC wrong-year repair:** 6,067.72/6,104.03 million USD belong to July 2025 and are explicitly inserted at 31 July 2025. May 2026 keeps the accepted June-MEI vintage 6,212.75/5,837.67. June becomes 6,198.59/7,116.88; July 2026 becomes the WSEI 6,901.7/6,279.3. July-MEI revisions of May to 6,422.17/5,864.42 remain an unresolved revision proposal, not silently accepted.
- **Borrowing:** May is July–May FY26; June is the full FY26. Bank/nonbank/domestic/foreign June values are 165,538.20/−847.17/164,691.03/57,743.60 crore. The 118,000 budget target is not an observation. Bank, domestic and foreign aliases are recomputed in BDT trillion (crore ÷100,000), preserving negative nonbank repayment.
- **Aliases and ratios:** five relevant banking aliases and three fiscal conversions are rebuilt from the same verified parent period. CRR-labeled balance/deposit ratios are 5.6486% for May and 5.1012% for June. No July ratio is manufactured. SLR and its excess-liquidity numerator/alias have no verified period in this package: their rows are proposed for exclusion and remain unavailable until evidence is recovered. This is a product availability decision still requiring approval.
- **GDP:** verified FY25 revised 3.49% and FY26 provisional 4.14% move to `gdp_growth_fy_pct`, with fiscal years ending 30 June. The legacy `gdp` definition is deprecated; its 126 daily stamps remain only in backup. There is no inference of historical periods from counts or equal values.
- **IMF/MoF:** 29 explicitly IMF-sourced mixed-namespace rows and six separately identified projection rows are archived/excluded by exact key. Future forecasts are retained as forecast evidence in backup; their numbers are not shifted into observed history. The raw historical IMF numerical document is absent, so no historical values are promoted under the new IMF ID. Five EconDelta/MoF candidate rows remain unchanged and unresolved; no claim is made that their dates are verified. The future-date sentinel exceptions remain until approved cleanup/read-back.
- **BPM6:** only the monthly definition label/description changes to “FX reserves (BPM6 gross)”. Stable ID and all values stay unchanged. The preserved official BB reserves HTML and the accepted E4 semantic contract support this.
- **Auction May:** eight source dates change from month-first placeholders to source **issue/settlement** dates, with identical cutoff yields and monthly buckets. The preserved BB source is a real capture from the original auction scraper commit. It does not establish the auction-held dates. The sixteen June/July candidates have only backup corroboration: they remain unchanged and unresolved. No one-day subtraction.
- **CPI:** disputed provenance/values remain unchanged. A new trusted-source label does not retrospectively validate a disputed reading.
- **EPB/NBR:** accepted EPB-via-BSS monthly history remains unchanged; official July/August workbooks are not speculatively spliced. Frozen NBR monthly archive remains intact. Other slow-series histories outside these selected families remain unresolved/out of scope for this bounded manifest.
- **Published Briefs:** no existing editorial record is rewritten. The B1 crash/previous-edition visibility blocker remains R3-owned.

## Safety contract and operator sequence

1. Keep the original backup, source files and reviewed manifest immutable. Verify the manifest hash and every referenced file. Paths are part of the reviewed bytes: copying evidence to different absolute paths requires a newly reviewed manifest hash. Do not edit paths after approval.
2. `python -m scripts.history_repair_candidates --backup-dir PATH --source-root PATH --target supabase:ssbliukchgibjcjohibi --econdelta-commit FULL_SHA --brief-commit FULL_SHA --output candidate.json` only builds a proposal from the verified twelve-table snapshot. It cannot connect to a database.
3. `python -m scripts.repair_observation_history --plan --candidate candidate.json --target TARGET --expected-sha256 HASH --out-dir NEW_DIRECTORY` validates all references and writes a reviewable manifest; it is read-only.
4. Before any production action: separately approve exact operations/definitions/exclusions, deploy repaired producers/consumer, resolve R3 identity/visibility and other preconditions, pause every overlapping writer including direct writers/media/Mac sync/Brief publication, take final backup and recheck complete before-images. Offset paging can omit records during concurrent deletes; keyset paging can miss inserts below its cursor, and separate table reads are not one transaction. Pause writers before the final export and verify counts, unique keys and parent/child integrity against the frozen database. Routine concurrent backups require a transactionally consistent snapshot mechanism. Changed before-images require a fresh proposal/review, not a force flag.
5. `--apply MANIFEST --target TARGET --expected-sha256 HASH --out-dir RECEIPT_DIRECTORY --writers-paused` applies only exact reviewed keys. Production credentials come only from the approved environment. The exact project HTTPS hostname must match `SUPABASE_URL`. Never run two repair sessions with different receipt directories. Keep writers paused throughout: REST GET/PATCH does not provide a cross-request transaction.
6. Reuse the SAME manifest and receipt directory after interruption. An after-image without this session’s durable pre-write intent is a conflict. The receipt file is private, atomically replaced and flushed to disk before each mutation and after full-row read-back. Already-confirmed operations are checked but not rewritten.
7. `--restore RECEIPTS --target TARGET --expected-sha256 RECEIPT_HASH --out-dir PATH --writers-paused` freezes reverse operations, restores in reverse order and refuses a current row that differs from the expected after-image. It never overwrites a newer legitimate update. Reuse the same receipt hash to resume an interrupted restore; the separate `.restore.json` records progress. After restore begins, apply-resume is refused.

The Docker target has a separate reviewed hash and must explicitly name the existing container/database. It refuses a container with network access, exposed ports or a PostgreSQL network listener. It locks and compares each exact row inside a database transaction, preserving JSONB types. This is a rehearsal transport, not production infrastructure recovery.

## Verification boundary

Focused tests before rehearsal: EconDelta 37 passed, Brief exporter 15 passed. Tests cover server caps, wrong target/hash, full before-image conflict, occupied destination, duplicate operations/keys, missing evidence/backups, whole-batch preflight, crash after commit, resume, already-applied operations, Boolean/number and nested JSON distinctions, interrupted restore and refusing rollback after a newer update. Initial actual restore proof is already approved. R1 actual mutation/rollback results and final hashes are recorded in the private task report; final full-suite matrix belongs to R2. No production changes, model calls, email, Discord, pushes, merges or deployments were performed.

The first candidate and its receipts remain immutable under the original hash in the private workspace. The revised candidate changes exactly six after-images, solely their release-status provenance; code references and the manifest generation timestamp are updated. Proposed row ingestion timestamps retain the initial snapshot proposal stamp, so unrelated after-images remain byte-for-byte comparable. A recorded exact delta binds the full first-candidate mechanical rehearsal to a separately hashed six-operation revised-image rehearsal; the final baseline comparison must still pass. Neither candidate is approved for production.
