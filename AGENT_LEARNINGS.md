# Agent Learning Rulebook — EconDelta

A running log of lessons learned the hard way while shipping EconDelta.

Different from `AGENTS.md` — that file documents **stable conventions and landmines** (the codebase is structured this way; don't break it). This file documents **incidents and lessons** (this is what went wrong, and here's how to prevent recurrence).

**Author:** AI agents under Adnan's direction. Appended on every incident; entries are point-in-time observations that may go stale but the lesson stays.

## How to add an entry

When something ships broken, when a methodology gap is exposed, or when a smoke test catches a real bug:

1. Write the entry below using the template.
2. If the lesson generalizes across Adnan's other projects, also append to the global rulebook at `~/.claude/AGENT_LEARNINGS.md`.
3. Save to AI auto-memory at `~/.claude/projects/-Users-adnanrashid-Projects-clauding-lab-econdelta/memory/` so future Claude sessions inherit.
4. If the lesson is a stable codebase rule, distill into a numbered `AGENTS.md` landmine.

## Entry template

```markdown
## YYYY-MM-DD — Short title

**Trigger:** what surfaced the issue.

**What went wrong:** root cause in plain English; cite file:line if useful.

**Lesson:** the generalizable rule in one sentence.

**Prevention:** concrete steps (validator, smoke checklist, CI gate).

**Hotfix:** what shipped to resolve.

**Cross-references:** AGENTS.md landmine, auto-memory key, global rulebook entry.
```

---

## Entries (most recent first)

## 2026-07-09 — DSE feed dead 24 days: server sends an incomplete TLS chain, requests won't AIA-chase the intermediate

**Trigger:** Ecosystem-review handoff (E1.2). `dse_market` frozen in Supabase at 2026-06-11, `dse_dayend` at 2026-06-10, both while `run_logs` looked clean (index scraper "skips" on non-trading days; dayend all-fail was unalerted — see the E1.6 entry).

**What went wrong:** DSE renewed its cert ~2026-06-09. Its servers (`www.dse.com.bd`, `dsebd.org`) send only the leaf (`CN=*.dsebd.org`), OMITTING the issuing intermediate `Sectigo Public Server Authentication CA DV R36`. certifi trusts the *root* (`…Root R46`) but not that intermediate, and python `requests`/`urllib3` do NOT AIA-chase the missing intermediate the way browsers do — so `openssl s_client` returns `Verify return code: 21` and every DSE fetch dies with `CERTIFICATE_VERIFY_FAILED`. External cause (the server's chain), not our code, not a network block: `www.bb.org.bd` verified clean, box `ca-certificates` healthy.

**Lesson:** "Works in a browser, fails in requests" for TLS almost always means the server sends an incomplete chain and the client isn't AIA-chasing. The fix is to supply the missing intermediate — NEVER `verify=False` (that trades a fetch bug for a MITM hole). certifi having the *root* is not enough; the *intermediate* must chain.

**Prevention:** Vendored the intermediate at `fetchers/ca/sectigo_r36.pem` (the single canonical cert location, shared with `fetchers/tls.py`'s pre-existing host-scoped mof.gov.bd path — one file, one rotation point; the review caught that a first draft duplicated it under `certs/`) and merge it with certifi into one additive CA bundle (`utils/ca_bundle.combined_ca_bundle`) that `HttpClient` points `session.verify` at — additive so it's safe for every host and degrades to certifi-only on any failure. Tests pin the merge is additive, loadable, and wired in. Immediate hotfix appended the same intermediate to the venv's `certifi/cacert.pem` on ExonVPS + the Mac so the feed recovered ahead of the merge; that append is fragile to a certifi upgrade, which is why the repo bundle is the durable path.

**Hotfix:** PR (E1.2) `fix(http-client): bundle DSE's Sectigo intermediate`. Verified on ExonVPS BD egress: default certifi → SSLError on all 3 DSE hosts; combined bundle → HTTP 200 on all 3. `scrapers.dse_market` then ran clean (DSEX 5804.06, was 5516.82).

**Cross-references:** AGENTS.md landmine 33 (TLS incomplete chains); `utils/ca_bundle.py`; `fetchers/ca/sectigo_r36.pem` (+ `fetchers/tls.py`, the other consumer); the E1.6 entry below (dse_dayend had no alerting, which is why this stayed dark 24 days).

## 2026-07-09 — 22 indicators "frozen" for 34 days: a merge-upsert never bumped ingested_at

**Trigger:** Ecosystem-review handoff (E1.1). The inflation family, `deposits_of_the_system`, `money_multiplier`, debt stocks, and their Brief aliases all showed `max(as_of)=2026-06-05` / `max(ingested_at)=2026-06-05` in live Supabase — yet `parse_all` produced their values EVERY day.

**What went wrong:** After the `source_as_of` recovery (#64/#65), each affected metric is written to its recovered reporting vintage (e.g. `debt_domestic_stock_cr` → `as_of=2025-12-31`). The upsert `ON CONFLICT (metric_id, as_of) DO UPDATE` lands on the SAME static row every run — `value` updates in place, but `ingested_at` was NOT a posted column, so it kept the row's first-insert time (the column default `now()` fires only on INSERT, never on the UPDATE half of a merge-upsert). A pipeline writing daily therefore read as "stale for weeks" to any `ingested_at`-keyed freshness check, and `max(as_of)` stalled at the last pre-recovery daily row (2026-06-05). The vintage rule itself is CORRECT — `as_of` should stall when the source hasn't republished; the bug was that the *write* was invisible.

**Lesson:** With PostgREST `resolution=merge-duplicates`, a column you don't POST is not touched on conflict — so a "default now()" write-timestamp freezes the moment you stop posting it. If you need write-liveness to be observable independently of the business date, POST the timestamp explicitly every run.

**Prevention:** `_rows_from_data` now posts `ingested_at` on every row (defaults to `now()`, overridable for tests). `as_of` still stalls at the vintage; `ingested_at` advances each run. Regression test asserts a recovered-vintage row keeps its stalled `as_of` but gets a fresh, distinct `ingested_at`.

**Hotfix:** PR (E1.1) `fix(supabase-writer): post ingested_at so a merge-upsert bumps write-liveness`. NOTE: this also masks a deeper question returned to the owner — the underlying MEI issue the pipeline holds is April 2026 while BB has published May 2026 (a separate fetch/discover staleness), and the consumer read-rule for vintage-stamped ids (max-by-`as_of` vs max-by-`ingested_at`) needs an owner decision.

**Cross-references:** `utils/supabase_writer.py` `_rows_from_data`; relates to the #64/#65 `source_as_of` recovery (landmine 26) and the 2026-06-01 "2xx ≠ persisted" entry (both "the write looked fine but the freshness signal lied").

## 2026-07-09 — Pink sheet: 10/10 green runs writing nothing because the source URL was pinned to a stale edition

**Trigger:** Ecosystem-review handoff (E1.5). `world_bank_pink_sheet` `run_logs` all `ok`/exit 0 (~1.3s) while lng/palm/wheat held one row each frozen at `as_of=2025-12-31` since 2026-06-01.

**What went wrong:** `PINK_SHEET_URL` was pinned to the World Bank doc-id `…-0050012025`, whose `CMO-Historical-Data-Monthly.xlsx` ends at period `2025M12`. The scraper fetched it, parsed the latest month (Dec-2025), and upserted the SAME `(metric_id, 2025-12-31)` row every day — so it looked healthy while the *source edition itself* was frozen. Not deploy drift (box code current), not a no-op write (the write succeeds and matches the workbook). Confirmed by fetching that exact workbook: latest row `2025M12`, values identical to Supabase.

**Lesson:** A "download this file" URL with an embedded document/edition id is a silent staleness trap: the publisher rolls the id and the old file freezes at its edition's last period, so a healthy-looking daily job serves a fixed snapshot forever. Green runs + unchanging data = check whether the SOURCE moved, not just whether the write worked.

**Prevention:** Repointed to the current edition `…-0050012026` (verified live: latest `2026M06`, values in range, same "Monthly Prices" shape) and added a landmine note in the scraper. Durable fix (follow-up): discover the latest link from `worldbank.org/en/research/commodity-markets` instead of pinning; the E2 freshness sentinel also catches the next roll.

**Hotfix:** PR (E1.5) `fix(pink-sheet): repoint to WB 2026 edition`. Also benefits from the E1.1 `ingested_at` bump (the metric now advances in both `as_of` and freshness once deployed).

**Cross-references:** `scrapers/world_bank_pink_sheet.py` `PINK_SHEET_URL` note; sibling 2026-06-01 Tier-2 write-path entry (same "green but empty" shape, different cause).

## 2026-07-09 — "Fabricated CRAR = 1.56%" wasn't fabricated: 25-agent review convergence vs one primary-source read

**Trigger:** Ecosystem-review handoff (E1.3) asserted, with adversarial re-verification by an independent review agent, that `banking_sector_crar = 1.56` was a fabricated LLM-fallback value ("real BD CRAR is ~10-13%; 1.56 implies insolvency") and prescribed tightening `valid_range` + correcting the row.

**What went wrong (in the review, not the pipeline):** Opening BB's actual QFSAR PDF (Issue 33, Jul–Sep 2025) that the pipeline holds showed **1.56% is BB's own printed figure** — the system-wide *pre-shock* CRAR at end-Sept-2025, stated on p13 (Exec Summary: "the pre-shock Capital Adequacy Ratio (CAR) stood at 1.56 percent"), p38, p39, and the p40 stress-test table ("Pre-shock CRAR: 4.47 [Jun-25] | 1.56 [Sep-25]", "Required minimum 10"). It is exactly what the config `task` instructs the parser to extract. The wider distress picture corroborates it (gross NPL 35.73%, ROE −15.1%, quarterly net loss). The review's 25 agents converged on "fabricated" because they reasoned from the ~10-13% prior WITHOUT reading the stress-test chapter; the config's own `valid_range` was already `[-50, 30]` (deliberately widened to permit the sector's trajectory toward the negative CRAR press reports cite for Q4-2025). Had E1.3 been executed as prescribed, a tighter range would have SUPPRESSED a true, alarming figure and a "correction" would have FABRICATED a wrong one.

**Lesson:** AI convergence is correlated bias, not proof (CLAUDE.md rule 9). When many agents agree a number is "impossible," verify against the PRIMARY artifact before acting — a bad number and correctly-reported bad news look identical from the outside. The domain expert (and the source PDF) outranks converging AIs. Show the observation (the PDF quote) before the inference (CLAUDE.md rule 10).

**Prevention:** Do NOT tighten `banking_sector_crar` `valid_range` and do NOT "correct" the 1.56 row — both were premised on a false claim. The residual question (should the dashboard surface the stress-test *pre-shock* CRAR, or is a different headline figure expected?) is a metric-DEFINITION decision returned to the owner, presented not acted. The deterministic `pdf_component` parser failing → LLM fallback here is expected (landmine 26, prose exec-summary) and produced the CORRECT number.

**Hotfix:** None — the value is correct; the safe action was to change nothing and report. First-hand verification: `pdfplumber` extract of `data/_pdfs/banking_sector_crar/.../qfsar (july-september 2025).pdf` on ExonVPS confirmed the four in-document occurrences of 1.56%.

**Cross-references:** AGENTS.md landmine 26 (QFSAR LLM-fallback path); global `~/.claude/AGENT_LEARNINGS.md` (convergence-isn't-proof); relates to the 2026-06-04 NPL entry (same report, the 35.73% NPL).

## 2026-07-09 — The one scraper with no notifier import went dark for 24 days; the aggregate write-swallow was equally silent

**Trigger:** Ecosystem-review handoff (E1.6). `dse_dayend` failed every trading day for ~24 days and nobody was paged; the DSE TLS break (entry above) stayed invisible that whole time.

**What went wrong:** `scrapers/dse_dayend.py` + `scripts/backfill_dse_dayend.py` were the ONLY scrapers that imported no `utils.notifier` — every sibling calls `notify("error", …)` on failure. All-fail → `return 1` → `run_logs` `fail` → silence (no `OnFailure=` unit either). Separately, `aggregate_latest.main()` caught `SupabaseWriteError` with a bare `logger.warning` and continued (correctly, on the local archive) — but a rotated key / PostgREST outage would leave every consumer serving yesterday's data with zero signal.

**Lesson:** "Success measured at the wrong layer" — a scraper can exit 1 and log `fail` while nobody watches `run_logs`; a swallowed exception can be the *right* fallback and still need to be loud. Every failure path that a human isn't actively tailing needs an active alert, and "continue on fallback" is not a reason to stay silent.

**Prevention:** `run_backfill` gained `notify_on_failure` (True on the daily `dse_dayend` path, False for manual backfills/dry-runs) — fires an `error` alert on zero-tickers, a below-floor partial (25/30), or a `SupabaseWriteError` (then re-raises). `aggregate_latest`'s swallow path now `notify("error", …)` before continuing. Tests assert BOTH the exit code AND the notify for each path.

**Hotfix:** PR (E1.6) `fix(dse-dayend): alert on empty/below-floor write + swallowed Supabase failure`. This is a specific instance of the class Phase E2 (freshness sentinel + landed-count invariant) exists to kill wholesale.

**Cross-references:** `scrapers/dse_dayend.py`, `scripts/backfill_dse_dayend.py`, `aggregate_latest.py`; the E1.2 TLS entry above (the failure this silence hid).

## 2026-06-07 — Older MoF MFRs reflow their columns between issues; the dry-run + FYTD self-check caught it before any bad write (fiscal backfill)

**Trigger:** "F7" — backfilling FY24/FY25 monthly fiscal data (govt bank borrowing + NBR revenue) to deepen The Brief's fiscal charts. After shipping a per-fiscal-year anchor parser (PR #72), a full-archive `--static-only`-precursor `--dry-run` over the real MFR PDFs.

**What went wrong:** Two findings. (1) **Forward-fill was a no-op** — MoF's newest published MFR is Oct 2025 (~8-month lag), already in the DB; "stale at Oct 2025" was the source's publishing frontier, not a pipeline gap. (2) The per-FY value-anchor parser (find the current-FY annual *Budget* in the row, take the next two numbers as single/FYTD) works for FY26 but FAILS for FY24/FY25: those reports shift their Table-6/Table-4 column layout report-to-report — an inserted "Revised Budget FYxx" column, a dropped prior-FY full-year column, trailing memo columns — so `anchor+1/+2` lands on the wrong cells. Apr-2025 read 99,000 (the FY25 *revised budget*) as April's single-month with a YTD of 610.

**Lesson:** A value-anchor + fixed column offset is only as stable as the table's column COUNT; multi-year government PDFs reflow columns between issues. And a cross-issue FYTD self-check (single ≈ Δ cumulative) is a reliable validator for clean additive series (NBR reconciled **22/22**) but NOT for figures the source restates between issues (bank borrowing reconciled **0/20** — MoF revises borrowing). Same extraction logic, opposite trust.

**Prevention:** The FYTD self-check is a HARD GATE on the parser write-path (drops non-reconciling months; July/gap months kept); the dry-run prints every parsed month for human review BEFORE the VPS write. Layout-inconsistent historical data is loaded as a hand-verified static dict (`STATIC_*_MONTHLY` + `--static-only`), mirroring `ADP_VALUES`, with extraction cross-validated against the live FY26 DB values (Jul'25 borrow 2,862 / Oct 5,720). Restated/provisional series are tagged via the `source` field so downstream can flag them.

**Hotfix:** PR #72 (merge `957e68f`). NBR 24 mo (`mof_mfr_static`) + borrow 23 mo (`mof_mfr_static_provisional`, Aug-2024 omitted — its published single conflicts with the restated cumulative). Written to prod 2026-06-07; verified 28 NBR / 27 borrow rows Jul'23→Oct'25, FY26 rows untouched.

**Cross-references:** AGENTS.md landmine 32; auto-memory `project_econdelta_fiscal_backfill`; spec/plan `docs/superpowers/{specs,plans}/2026-06-06-fiscal-backfill-per-fy-anchor*`; relates to `project_econdelta_pdf_table_row_source_as_of` (the source_as_of-recovery work).

## 2026-06-05 — New test CI caught that the suite isn't fully hermetic: 2 fetcher tests need a real headless Chromium

**Trigger:** Adding the first backend CI (`.github/workflows/test.yml`, ruff + pytest on push/PR) during the repo-review fixes. A review agent had asserted "the suite is hermetic — no secrets needed" (true) and implied it was fully self-contained (not quite).

**What went wrong:** The CI's first run FAILED at pytest (ruff + install were green). Two `tests/test_html_fetcher.py` cases launch a REAL headless Chromium against a local `file://` page — they are not mocked. They passed on the Mac only because a browser was already installed; the clean runner had none (`BrowserType.launch: Executable doesn't exist …chrome-headless-shell`). So "passes locally" masked a non-hermetic dependency, and the agent's hermetic claim came from grepping for `monkeypatch`, not from actually running browserless.

**Lesson:** "Hermetic" is a claim you verify by RUNNING in a clean environment, not by reading the tests. The value of real CI is precisely that it runs where your local conveniences (an installed browser) are absent.

**Prevention:** `test.yml` installs the browser before pytest — `python -m playwright install --with-deps --only-shell chromium` (the headless shell is sufficient; the fetcher runs headless). Keep that step. Distrust "the suite is hermetic" until it has been run with no browser/system extras present.

**Hotfix:** Added the playwright-shell step; re-run green (794 passed / 5 skipped in CI). PR #70 / merge `c27143a`.

**Cross-references:** AGENTS.md landmine 30; auto-memory `project_econdelta_repo_review_fixes`.

## 2026-06-04 — Two latent date-recovery bugs caught in review BEFORE merge: host-gating + first-match

**Trigger:** Building PR 1 (add `source_as_of` recovery to the shared `pdf_table_row` parser). A premise-check during investigation and a 3-lens adversarial review workflow each caught a wrong assumption before it shipped.

**What went wrong:**
1. *Host gating (caught by checking the premise, not assuming it).* I almost gated report-detection on the URL host (`mof.gov.bd`). But `fetch_all.py:82` reassigns `url` to the discovered PDF link before `fetch_pdf`, so `FetchResult.source_url` is the RESOLVED link — for MoF a third-party object store (`objectstorage.…oraclecloud15.com`), not `mof.gov.bd`. Host gating would have returned `None` for every debt metric — a silent no-op that looks like success.
2. *First-match stale-date (caught by the adversarial review's idiom-robustness lens).* The first draft used `re.search()` (first match) over the full PDF text. These gov reports print comparison/prior dates (`as of 30 June 2024`, `up to Jun FY25`); first-match can lock onto the STALE one — the exact NPL-class failure the PR exists to prevent. The reviewer reproduced it: `"as of 30 June 2024 … as of 31 December 2025"` → returned `2024-06-30`.
3. Smaller, same review: malformed-day fall-through (`31 February` borrowing an unrelated FY label's month) and an over-broad `"debt bulletin"` content marker (the parser actually serves 4–5 families incl. a separate MoF fiscal page).

**Lesson:** When a parser is shared across sources, the discriminator must be the PDF's own CONTENT (its title), not a URL/host the fetch layer may have rewritten; and date recovery from multi-period documents must take the LATEST match, never the first.

**Prevention:** (a) Before gating on any field, verify what that field actually holds at the point the parser sees it (here: trace `source_url` back through `fetch_all`). (b) For any "recover a date from a document" task, assume comparison/historical dates exist and pick the latest valid match. (c) Adversarial review (multiple independent lenses + per-finding verification) earns its cost on exactly this class of plausible-but-wrong code. (d) Validate against the REAL artifacts across multiple issues (checked MoF issues 15/16/17), not one synthetic fixture.

**Hotfix:** Shipped correct from the start (caught pre-merge). PR 1 / merge `6a42a15`: content-marker gating, `finditer` + latest-match, malformed-day → `None`, tightened marker to `"quarterly debt bulletin"`. 794 tests pass; live-validated.

**Cross-references:** AGENTS.md landmine 29; auto-memory `project_econdelta_pdf_table_row_source_as_of`; relates to the NPL stale-`source_as_of` fix (#64/#65, landmines 26/28).

## 2026-06-04 — Approve/reject loop was broken for every future run: digest numbered by list index, not `media_review.id`

**Trigger:** A 5-lens adversarial spec review (run as a Workflow BEFORE implementing the media-screen daily-report feature) — the integration lens flagged a CRITICAL by tracing the `approve N` identifier end-to-end across systems.

**What went wrong:** `media_screen/digest.py` numbered candidates by their position in the digest (`enumerate` → `i+1`) and instructed *"Reply `approve N`"*. But `media_screen/decide.py` and Copotron interpret `N` as the `media_review.id`. `insert_media_review_rows` posted with `Prefer: return=minimal` and returned a count, so the real ids never reached the digest. The loop was wired + "tested" the prior session and *appeared* to work — but ONLY because the first-ever `media_review` rows had ids 1, 2, coinciding with digest positions 1, 2. On the next real candidate (ids ≥ 3) a reply of `approve 1` would PATCH `media_review.id=1` (the old, already-applied NPL row) → a silent no-op, or worse, approve the wrong row. The whole #thebrief approve loop would have silently failed in production.

**Lesson:** A feature can pass a single happy-path live test *by coincidence* (here: ids == positions on the very first run) while being broken for every subsequent run. When a value shown to a human is used as a command key downstream, it MUST be the real persistent key (the DB id), never a presentation-order index. A green live test ≠ correctness — trace identifiers end-to-end across system boundaries.

**Prevention:** `insert_media_review_rows` now returns the inserted ids (`Prefer: return=representation`, `?select=id`); `run_screen` inserts kept candidates FIRST, then `format_report(zip(ids, candidates), …)` numbers each line by the real id; the digest reads `approve <id>`. Unit tests assert the integer in the message equals the inserted `media_review.id`, not the loop index (`test_candidate_uses_real_id_and_approve_reject`, `test_insert_returns_inserted_ids`). Methodology: run an adversarial multi-lens review of the SPEC before building, with one lens that traces each identifier/contract across files and systems — it caught this; the per-task reviews alone would not have (they see one task in isolation).

**Hotfix:** Folded into the media-screen daily-report feature (merged `6fa68b5`): `d49e58d` (insert returns ids), `a63849a` (format_report numbers by id), `dd46b1a` (run_screen insert→zip→format).

**Cross-references:** spec `docs/superpowers/specs/2026-06-04-media-screen-daily-report-design.md`; `docs/media-screen-copotron-wiring.md`; auto-memory `project_econdelta_media_screen_npl`; global `~/.claude/AGENT_LEARNINGS.md` 2026-06-04 (live-test-by-coincidence). **Candidate AGENTS.md landmine:** the media-screen digest numbers by the real `media_review.id`; `insert_media_review_rows` must return ids.

## 2026-06-04 — Brief served a stale NPL (35.73% vs press 32.26%): correct value, missing `source_as_of`, nothing could supersede it

**Trigger:** Fact-checking The Brief — it showed `banking_npl_pct` = 35.73% while the BD press (Daily Star / TBS) reported 32.26%. Investigation on the ExonVPS: the cached BB QFSAR PDF is `qfsar (july-september 2025).pdf`.

**What went wrong:** Two compounding bugs. (1) **`source_as_of` was never recovered on the LLM-extract path.** The QFSAR is prose ("as of end-September 2025" / "July-September 2025"), so the deterministic `pdf_component` parser fails on it and the value comes back via the LLM fallback (`provenance=llm_extracted`). `source_as_of` recovery only ran on the deterministic path; `_extract_quarter_end`'s regex only matched "Quarter ending DD Month YYYY" → **0 matches across 71 pages**. So a *correct* Q3-2025 value (35.73%) landed with NO reporting date. Supersession is date-driven, so an undated row can never be replaced by a newer release → the faster end-Mar-2026 BB figure (32.26%) couldn't take over, and 132 daily-dated junk rows piled up. (2) **The alias the Brief actually reads wasn't propagated.** Fixing `gross_npl_ratio` wasn't enough — `builders/banking.py` reads the derived alias `banking_npl_pct`, and `source_as_of` wasn't carried through `BRIEF_ALIASES` / `BRIEF_CONVERSIONS`. Verifying the *real read key* (not the source key) is what caught it.

**Lesson:** A correct value with a missing/wrong `source_as_of` is worse than no value — it serves stale data indefinitely because supersession is date-driven and nothing can replace an undated row. Slow-cadence metrics that fall to the LLM path need date recovery on THAT path, plus a guardrail that warns when one lands undated. And when verifying a downstream display bug, trace the EXACT key the consumer reads (often a derived alias), not just the source metric.

**Prevention:** PR #64 — broadened `_extract_quarter_end` + `recover_source_as_of`, wired into `hybrid.parse_one`'s LLM fallback, and `_build_source_as_of_map` now warns on any undated slow-cadence metric (it flagged ~9 others — `debt_gdp_ratio`, `gdp`, `fy_export`, `categorywise_export`, `fy_import_lc`, `fy_remittance`, debt stocks — whose parsers still lack date recovery; tracked separately). PR #65 — propagate `source_as_of` through `BRIEF_ALIASES` / `BRIEF_CONVERSIONS`. The daily human-gated **media-screen** (PRs #66-68) is the general fix for "the press reports the new quarter before BB's slow pipeline catches up": it queues a period-pinned press value for owner approve/reject and applies it as a temporary bridge until BB's own release supersedes it.

**Hotfix:** Deleted 132 daily-dated junk rows (`as_of > 2025-09-30` for the 4 NPL/CAR metrics); re-extract recovered `2025-09-30`; then the live media-screen override set `gross_npl_ratio` AND `banking_npl_pct` = 32.26 @ 2026-03-31 (`source=media-approved:thedailystar`), history retaining `2025-09-30: 35.73`.

**Cross-references:** PRs #64/#65 (source_as_of recovery + alias propagation), #66-68 (media-screen); `docs/media-screen-copotron-wiring.md`; auto-memory `project_econdelta_media_screen_npl`; global `~/.claude/AGENT_LEARNINGS.md` 2026-06-04; sibling lesson 2026-06-01 (`project_econdelta_tier2_writepath_fix`, "2xx ≠ persisted") — both are "the write looked fine but the data was wrong."

## 2026-06-02 — OMO scalars (slf_draw_cr / bb_repo_usage_cr) retired: walled-PDF only, no HTML route-around

**Trigger:** Follow-up to the auction-tables fix. These two scalars sourced from BB's "Open Market Operations as on <date>" press release (the old combined "Result of the Auction of Repo, ALS, SLF, SDF and IBLF" release), and had never landed (0 rows since launch).

**What went wrong:** Same restructure as the auction tables — the OMO release became a PDF behind the F5 + image-CAPTCHA wall. But UNLIKE the auction results/calendar (which had clean HTML alternatives on `treasury` / `auc_calendar/1`), the SLF/Repo *accepted amounts* have NO HTML source: checked `call_money_market` (interbank call money, not BB ops), `mptools` (prose describing the corridor — the *rates* land via `policy_rate_*`), `money_market_ref_rate` (DOMMR/BOFR products), and every `monetaryactivity/*` + `financialactivity/*` nav page. The wall itself is genuinely intractable: 7 retrieval methods failed, and an instrumented attempt showed the in-iframe CAPTCHA re-serves a **byte-identical** challenge — the `#jar` submit never advances, so even a correct answer (`claude-sonnet-4-6` read the refresh-icon correctly) does nothing. Structural, not model-accuracy.

**Lesson:** "Route around the wall" only works if an accessible alternate actually exists — verify it does before promising a fix. When the data is genuinely PDF-only behind an unsolvable wall, the honest move is to RETIRE the metric (and its now-dead subsystem), not to carry never-working code or chase the wall indefinitely. Time-box wall spikes; a byte-identical re-served challenge across attempts = structural dead end, stop.

**Hotfix:** PR #62 — removed `slf_draw_cr` + `bb_repo_usage_cr` from `sources-v3.json` and deleted the last-consumer `/rrpt/` subsystem (`fetchers/rrpt_discovery.py`, `parsers/html_auction_press_row.py` + prompt, the `fetch_all` `latest_rrpt_link` branch + `_download_rendered_html`, tests). 642 deletions; 679 tests pass.

**Cross-references:** AGENTS.md landmine 24; auto-memory `project_econdelta_r2_auction_html_sources`; the auction-restructure entry below.

## 2026-06-02 — BB retired the auction RESULTS + CALENDAR sources behind a CAPTCHA wall; PR #48's tables stayed empty

**Trigger:** Asked to "rewrite `discover_latest_rrpt_link` + re-enable `auction.timer`." A read-only box dry-run showed the solver-CLEARED press-release listing had ZERO `/rrpt/` anchors — so discovery was not the bug; the SOURCE had moved.

**What went wrong:** BB restructured both auction sources. (1) RESULTS: the per-business-day `/rrpt/` press release became a PDF (`mediaroom/press_release/press/pr<id>_<date>.pdf`) behind an F5 BIG-IP + image-CAPTCHA wall that does NOT yield to the Chromium+haiku solver — five retrieval methods (top-nav, `ctx.request`, in-page `fetch`, iframe-`src`, in-iframe CAPTCHA solve) all returned the wall HTML, never `%PDF`. (2) CALENDAR: `auc_calendar` stopped rendering a server-side `<table>` (0 tables even with a 20s `table` wait-selector); the forward strip moved to `auc_calendar/1` ("Yearly calendar") as a CSS div-grid (`div.row-header` + `div.row-data` / `div.column`). Both `auction_results` and `auction_calendar` (added in PR #48) had therefore stayed empty since launch.

**Lesson:** When a scraped source returns nothing, check whether the SOURCE moved before "fixing" the parser/discovery — and when the new source sits behind a hard wall, hunt for an already-accessible alternate BEFORE sinking effort into defeating the wall. Here the same data was on `monetaryactivity/treasury` (HTML, already lands the scalar cut-off yields) and `auc_calendar/1`. Route-around beats defeat-the-wall.

**Prevention:** Verify-first on the live box (read-only fetch+parse dry-run) before any source-scraper rewrite — the dry-run revealed the restructure AND later caught the PGRST102 write bug. New fixtures are REAL box captures (`tests/fixtures/bb_treasury_auctions.html`, `bb_auction_yearly_calendar.html`), not synthetic.

**Hotfix:** PR #59 — `parse_treasury_results` (group-aware 2-row-header table) + `parse_yearly_calendar` (document-order bills+bonds div-grid); repointed `AUCTION_RESULTS_URL` → `treasury` and `AUCTION_CALENDAR_URL` → `auc_calendar/1`; removed the dead `/rrpt/` results path + table-based `parse_auction_calendar`. Live: 8 results + 17 calendar rows landed + verified in Supabase; `auction.timer` re-enabled.

**Cross-references:** AGENTS.md landmine 24; auto-memory `project_econdelta_r2_auction_html_sources`; the PGRST102 entry directly below; prior `2026-06-01` Tier-2 write-path entry.

## 2026-06-02 — First real auction_results write rejected: PostgREST PGRST102 "All object keys must match"

**Trigger:** First live `econdelta-auction.service` run after PR #59 deployed: `results: parsed 8 row(s)` then `SupabaseWriteError: HTTP 400 PGRST102 "All object keys must match"` — 0 results written, while the 17-row calendar batch (homogeneous keys) landed fine the same run.

**What went wrong:** PostgREST bulk-upsert (a POSTed JSON array) requires every object to carry the SAME keys. Auction RESULTS rows are heterogeneous — bond rows have `wam`, bills don't — so the whole batch 400'd. `utils/supabase_writer._validate_auction_rows` normalised each row individually but never reconciled the key SET across the batch. The bug was latent until now because `auction_results` had literally never been written (its source was broken — see the entry above).

**Lesson:** A parsed batch ≠ a writable batch. For a PostgREST array upsert, EVERY row must have an identical key set — union the keys across the batch and fill missing ones with NULL before POST. (Companion to the prior "2xx ≠ persisted" lesson: call this "uniform-keys-or-400".)

**Prevention:** `_validate_auction_rows` now unions all keys present across the batch and `setdefault`s each missing column to `None` (a real SQL NULL, never a fabricated value); a no-op for single-row / already-homogeneous batches. Test: `test_heterogeneous_rows_get_a_uniform_key_set`.

**Hotfix:** PR #60. Re-ran the service: 8 results + 17 calendar rows upserted, exit 0; verified in Supabase (bills `wam`=NULL, bonds `wam` set).

**Cross-references:** AGENTS.md landmine 25; auto-memory `project_econdelta_r2_auction_html_sources`; global `~/.claude/AGENT_LEARNINGS.md` (PGRST102 uniform-keys).

## 2026-06-01 — Tier-2 scrapers logged "upserted N rows" but wrote to the SOURCE host, not Supabase

**Trigger:** Completing the PR #48 "deploy & land" runbook. After deploying, the three standalone Tier-2 scrapers ran with `result=success`/`exit=0` (imf_eff), an Adobe-Helix `404` (pink-sheet), or a 2-min systemd timeout (imf_debt) — yet `metric_history` stayed **empty** for all of their metric_ids. imf_eff even logged "upserted 1 imf_eff_outstanding_sdr_mn row" while the table had **0 rows**.

**What went wrong:** Two layered bugs, fixed in sequence:

1. **IMF fetch hung on a blackholed IPv6 (PR #54).** `www.imf.org` + `thedocs.worldbank.org` have their IPv6 (AAAA) addresses **blackholed from the ExonVPS box** (`curl -6` times out at 25s; `-4` ~1.5s). `imf_eff`/`imf_debt_gdp` fetched over dual-stack, stalled on the dead AAAA, and were killed before any write (`run_logs` showed `status=running`, never finished). `world_bank_pink_sheet` already forced IPv4 but never restored urllib3's process-global `HAS_IPV6` (a bleed). Fixed with a shared `utils/ipv4.force_ipv4_only()` guard (save → set False → restore in `finally`) around each fetch.

2. **The upsert was misrouted to the source website (PR #55) — the real reason nothing landed.** `imf_eff`, `imf_debt_gdp`, and `world_bank_pink_sheet` passed the source-page URL as `upsert_metric_history(url=...)`. But that kwarg is the **Supabase base-URL OVERRIDE** (`_resolve_credentials: url or os.environ["SUPABASE_URL"]`), so every metric write was POSTed to the source host: pink-sheet → `thedocs.worldbank.org/…/rest/v1/metric_history` → **404 (Adobe Helix)**; imf_eff → `www.imf.org/…/rest/v1/metric_history` → **301 → 2xx page → false "upserted" with no persistence**; imf_debt → 23 POSTs/run to imf.org → slow → timeout. `run_logs` writes used the env URL (no override), so they landed — which is exactly why `run_logs` rows persisted but `metric_history` did not, in the same run. `aggregate_latest` never passes `url=`, so the main pipeline was always fine.

**Lesson:**
- **A 2xx response / a "wrote N rows" log is NOT proof of persistence.** Verify the row actually landed (re-query the table), not that the POST "succeeded" — a redirect to the wrong host returns 2xx and silently drops the data.
- **A kwarg that overrides a base URL or credential is a footgun when it looks like a provenance field.** `upsert_metric_history(url=...)` reads as "the source URL of this data" but means "the Supabase endpoint to write to." Don't pass source/provenance URLs there.

**Methodology lesson (this nearly went wrong):** the Adobe-Helix 404 + intermittent-looking failures looked exactly like an **ISP-level DNS/SNI hijack of `*.supabase.co`** from the Dhaka box, and I was one step from escalating that to Adnan as the root cause. An 80× direct probe to the *correct* Supabase endpoint came back 100% clean — that mismatch (a "network hijack" should hit the direct probe too) is what forced a re-read of the code and surfaced the deterministic `url=` bug. **When an inference doesn't fit a cheap direct test, re-derive from first principles before blaming infra.**

**Prevention:** Per-scraper regression test that the upsert wrapper passes **no `url` override** (`tests/test_{imf_eff,imf_debt_gdp,world_bank_pink_sheet}.py::test_upsert_does_not_override_supabase_url`) + `tests/test_ipv4.py`. Consider a post-run smoke that re-queries `metric_history` count for a scraper's ids after a live run (a "wrote N" log alone is insufficient). The existing upsert unit tests mocked `upsert_metric_history` entirely, so the misrouting (which lives *inside* it) was never exercised — mock one level deeper or assert the destination.

**Hotfix:** PR #54 (IPv4 fetch guard) + PR #55 (remove the `url=` override). Verified on the box: all 5 metrics now land (`debt_gdp_ratio`=29 rows, `imf_eff_outstanding_sdr_mn`, `lng/wheat/palm`). 3 timers re-enabled (`auction` stays disabled — separate `/rrpt/` restructure).

**Cross-references:** `utils/ipv4.py`; auto-memory `project_econdelta_tier2_writepath_fix`; global `AGENT_LEARNINGS.md` 2026-06-01 (2xx≠persisted). Residuals (R1 mof.gov.bd config metrics, R2 auction) tracked separately.

## 2026-06-01 — Adopting Supabase CLI migrations exposed a co-mingled shared DB + a `db push` dead-end

**Trigger:** Setting up CLI-managed migrations (move `db/migrations/` → `supabase/migrations/`, apply the long-stuck `0009` auction tables). After linking the CLI to the shared project `ssbliukchgibjcjohibi` and running `supabase migration list --linked`, the remote history was NOT the assumed clean EconDelta history — it held ~28 timestamped migrations, and `db push` flatly refused to run.

**What went wrong:** Two distinct findings, both contradicting the session-handoff assumptions:

1. **Co-mingled schema from MCP `apply_migration`.** The project held 12 abandoned **Notifyr** tables (`rm_sessions`, `otp_queue`, `message_templates`, … — all 0 rows) + 14 Notifyr migration-history rows, left behind because past agent sessions ran the Supabase **MCP `apply_migration` against whatever project was linked at the time** — this shared project rather than Notifyr's own (`ywdrprqnykxwkbthvmri`). Leftover `otp_queue`/`rm_sessions` even carried wide-open `anon` SELECT/INSERT/UPDATE (`using(true)`) policies on a project whose anon key ships in the public PWA — a latent footgun (empty → not a live leak). The handoff note "remote migration history is empty (all manual applies)" was simply wrong.
2. **`supabase db push` is incompatible with a shared multi-app DB.** Push (even `--include-all`) aborts with *"Remote migration versions not found in local migrations directory"* — it requires THIS repo to hold the database's ENTIRE history, but the DB is shared with The Brief, whose migrations are not in this repo. No single repo owns the full history, so push can never reconcile.

**Lesson:**
- (1) The Supabase MCP `apply_migration` writes to whatever project is currently **linked** — an easy way to contaminate the wrong/shared DB. Before any migration-tooling work, AUDIT THE ACTUAL DB CONTENTS (`pg_tables`, `schema_migrations` names, anon `pg_policies`) — never trust the assumed architecture or a prior session's note.
- (2) On a DB shared by multiple apps, `db push` does not work. Apply with `supabase db query --linked -f <file>`, keep migrations idempotent, and treat the git migration files (not `schema_migrations`) as the source of truth.

**Prevention:** AGENTS.md + db/README now state "apply via `db query --linked -f`, NOT `db push` (shared DB)". Verify `supabase/.temp/project-ref` before any `apply_migration`/`db push`. Note: the auto-mode classifier correctly BLOCKED the agent from self-executing the irreversible DROP on shared prod when the agent self-determined the "no impact" safety condition — irreversible shared-infra changes were routed to the user. Keep that split: agent prepares + verifies, user pulls the trigger.

**Hotfix:** Verified Notifyr lives on its own project + 12 tables 0-row + no consumer refs; DDL-snapshotted; user ran a transactional DROP (12 tables) + DELETE (14 history rows); applied `0009` via `db query -f` (auction tables live, anon-readable); migrations relocated; docs corrected.

**Cross-references:** AGENTS.md migrations landmine + `supabase/` repo-map line; db/README "Applying migrations"; auto-memory `project_econdelta_supabase_shared_db_cleanup`; global `AGENT_LEARNINGS.md` 2026-06-01.

## 2026-05-31 — IMF DataMapper (Akamai) BLOCKS spoofed browser UAs — opposite of BB's CAPTCHA wall

**Trigger:** Building the IMF debt/GDP history scraper (`scrapers/imf_debt_gdp.py`, plan S4). The initial implementation cargo-culted a `Mozilla/5.0 ... Chrome` browser User-Agent (the reflex from BB/DSE scrapers that need a browser UA to get past their bot walls). The live no-egress smoke run returned **HTTP 403 "Access Denied"** from `www.imf.org/external/datamapper/api/v1/...`.

**What went wrong:** The IMF DataMapper API sits behind **Akamai EdgeSuite**, whose bot rules do the OPPOSITE of BB's CAPTCHA: a spoofed browser UA is treated as bot-evasion and rejected (403), while an HONEST non-browser client (`curl/8.7.1`, python-`requests` default `python-requests/x.x`) is allowed (HTTP 200, full JSON). I had verified the source earlier with a bare `curl` (default curl UA → 200), then "hardened" the scraper with a browser UA and broke it. Two further traps: (1) the API ignores the `/COUNTRY` path segment and returns ALL 226 countries — filter `.values.<INDICATOR>.<ISO3>` client-side; (2) the `?country=BGD` query param is rejected by the same WAF.

**Lesson:** Egress walls are not uniform — do NOT assume the BB browser-UA trick generalizes. For a new source, verify the EXACT client signature that earned the 200 (UA, headers, path vs query) and encode THAT, not a habit from a different publisher. A spoofed browser UA can be the thing that gets you blocked.

**Prevention:** `fetch_imf_payload` sends NO custom User-Agent (requests default) with a comment forbidding a browser UA. Live no-egress smoke (`fetch_imf_payload()` + `parse_imf_series()`) is part of the S4 verification and would have caught this immediately had it run before the UA was added — run the live fetch for any NO-egress source before committing, not just the mocked unit tests.

**Hotfix:** Removed the `_BROWSER_UA` constant + header from `scrapers/imf_debt_gdp.py`; live fetch then returned 29 years (2003-2031) of real BGD data (2024 = 41.0%).

**Cross-references:** plan S4; landmine G/E adjacency (HTML cleaning / header-match); global `AGENT_LEARNINGS.md` (if promoted: "verify the exact 200 signature per source; browser-UA is not universal").

## 2026-05-30 — Weekly-briefing freshness gate passed when a CORE metric had ZERO history rows

**Trigger:** Adversarial final code review of the weekly-briefing branch (PR #39) flagged a CRITICAL: `briefing/freshness.assess_freshness` could return `core_stale=False` when a core series was entirely absent from `metric_history`.

**What went wrong:** The gate iterated only metrics PRESENT in `latest_as_of_by_metric` — built from `_collect_history`, which skips metrics returning zero rows. A core metric with a total Supabase/scraper gap was never evaluated, so it couldn't trip the gate; the briefing would generate against silently-missing core data. Staleness logic was right for present-but-old metrics but had no concept of "absent".

**Lesson:** A freshness/completeness check that iterates "what's present" cannot detect "what's missing." When a set of REQUIRED keys must all be fresh, iterate the required set and treat absence as the worst case — don't only iterate what arrived.

**Prevention:** `assess_freshness` now loops `core_ids` and sets `core_stale=True` for any id absent from history (test `test_absent_core_metric_trips_gate`). Meta-lesson: a whole-branch final review is non-optional — the per-task spec/quality reviews never ran for this module because the execution workflow died before reaching it (see global `AGENT_LEARNINGS.md` 2026-05-30).

**Hotfix:** `briefing/freshness.py` absent-core loop + test (commit `3e039f8`).

**Cross-references:** PR #39; global `AGENT_LEARNINGS.md` 2026-05-30 (workflow fragility — why per-task review coverage was incomplete); AGENTS.md landmines 18-20.

## 2026-05-29 — parse.service down for days: `claude` writes `~/.claude.json`, blocked by `ProtectHome` sandbox

**Trigger:** All daily EconDelta metrics in Supabase were stale since 2026-05-25 (newest on-disk snapshot for every daily indicator = 2026-05-25); `run_logs` showed `econdelta-parse.service` `status=fail exit_code=1 error=null` on every cron run.

**What went wrong:** `parse_all.main()` aborts (`return 1`) when `_claude_preflight()` fails. The preflight (`claude --print`) was exiting 1 with `API Error: EROFS: read-only file system, open '/home/adnan-local/.claude.json'`. The service runs under `ProtectHome=read-only` with `ReadWritePaths=… /home/adnan-local/.claude` — which carves out the `.claude/` **directory** but NOT the sibling `.claude.json` **file**. The `claude` CLI writes `~/.claude.json` (project history, startup counter, etc.) on each run; under read-only `$HOME` that write fails → preflight fails all 3 attempts → parse aborts before producing any snapshot. The 2026-05-17 fix had carved out `.claude/` for the OAuth credential refresh, but the CLI also writes the top-level `.json`. The same gap silently disabled `aggregate`'s Opus review (`opus review skipped: review_skipped: claude_exit_1`), so daily data was being written unreviewed.

**Diagnosis trap (cost me 3 wrong hypotheses):** the real error was in `logs/parse-systemd.log` (the unit's `StandardError=append:…`), NOT journald — so `journalctl` greps came up empty and I first suspected the OAuth token, then the model name, then "intermittent claude availability". Manual/`systemd-run` repros PASSED because they omitted the sandbox OR `.claude.json` had no pending write at that instant (the failure is state-dependent on whether claude needs to persist). Only reading the redirected log file surfaced the EROFS.

**Lesson:** When a hardened systemd unit (`ProtectHome=read-only`/`ProtectSystem=strict`) shells out to a stateful CLI, that CLI may write config OUTSIDE your carve-outs — and a directory carve-out does NOT cover a sibling file. Also: find a sandboxed service's real errors in its `StandardError=`/`StandardOutput=` target, not journald.

**Prevention:** Probe writability under the real sandbox: `systemd-run -p ProtectHome=read-only -p ReadWritePaths=… --uid=adnan-local python3 -c "open('/home/adnan-local/.claude.json','r+')"` → expect success (got `OSError 30 Read-only file system` before the fix, `WRITABLE` after). Any service invoking `claude` needs BOTH `~/.claude/` and `~/.claude.json` in `ReadWritePaths`.

**Hotfix:** systemd drop-ins `/etc/systemd/system/econdelta-{parse,aggregate}.service.d/10-claude-json-writable.conf` adding `ReadWritePaths=/home/adnan-local/.claude.json`; `daemon-reload`; `reset-failed`; restart. Parse warmup then `exit=0` and the run produced fresh snapshots. **NOTE:** the repo `deploy/` files were STALE (used `/home/adnan`, and the drop-ins lived only on the box) — reconciled in PR #37, so `deploy/` now matches the live units and a from-scratch redeploy is reproducible (paths, `.claude`/`.claude.json` carve-outs, retry-timer enablement all corrected).

**Cross-references:** auto-memory `project_econdelta_parse_401.md` (this is the 2026-05-25 regression of the 05-17 OAuth fix — same "preflight fails" symptom, different write path); global `~/.claude/AGENT_LEARNINGS.md` 2026-05-29; corridor entry below (same triage session).

## 2026-05-29 — New parser file shipped without wiring it into `parse_all`'s import block

**Trigger:** YieldScope's CorridorViz (PR #4) rendered "Demo", not live data. Triage found `policy_rate_repo/sdf/slf` had **0 rows** in Supabase despite PR #30 deploying cleanly to ExonVPS (HEAD `9142807`).

**What went wrong:** PR #30 added `parsers/pdf_table_column_latest.py` (with its `@register("pdf_table_column_latest")` decorator) plus three `sources-v3.json` entries, but never added `import parsers.pdf_table_column_latest` to `parse_all.py`'s "auto-import all parser modules so they register" block (lines 17-26). The decorator only runs on import, so in production the parser was absent from `REGISTRY`. Every scheduled parse logged `parse_one raised for policy_rate_repo: "no parser registered for 'pdf_table_column_latest'; have: [...9 others...]"` (caught + skipped per-indicator). Being brand-new metrics, the corridor rates had no prior snapshot for `aggregate` to carry forward → 0 Supabase rows. The parser code itself was correct: direct invocation on the fetched April-2026 MEI PDF returned 10.00 / 7.50 / 11.50.

**Lesson:** A parser/plugin that self-registers via an import-time decorator is invisible in production until something actually imports the module. Shipping the file + the config entry is not enough — the module must be wired into the entry point's explicit import list.

**Prevention:** Added `tests/test_parser_registry_coverage.py` — a **subprocess-isolated** test asserting every `parse.deterministic` in `sources-v3.json` is present in `REGISTRY` after importing `parse_all`. It runs in a fresh interpreter so it exercises parse_all's OWN import block, not registration leaked in by sibling test modules. PR #30's 26 tests passed precisely because they import the parser module directly (triggering `@register`), masking the missing production wiring — this test closes that blind spot.

**Hotfix:** One-line add of `import parsers.pdf_table_column_latest  # noqa: F401` to `parse_all.py`'s auto-import block (this PR), plus the regression test above.

**Cross-references:** PR #30 (`121969b`, introduced the gap); YieldScope PR #4 (blocked consumer); global `~/.claude/AGENT_LEARNINGS.md` (generalizes to any decorator-registry import-wiring). **Separate finding, deferred:** `parse_all.main()`'s Claude preflight gate aborts the *entire* parse — including deterministic parsers that need no LLM — whenever the `claude` CLI is briefly unreachable. Result: parse emitted **no new snapshots 2026-05-25 → 05-29**, so daily Supabase values are stale carry-forwards. Flagged as an architectural decision, not patched here.

## 2026-05-28 — A warn-on-X observability rule needs an allow-list of routine non-X

**Trigger:** Multi-agent review of PR #33 (`logger.warning` at the writer's non-scalar drop branch) caught that the warning would fire on every successful aggregate run.

**What went wrong:** PR #33's stated purpose was "surface the next PR #31-class silent drop on day 1". The implementation added an unconditional `logger.warning` at the non-scalar branch of `_rows_from_data`. But the `data` dict reaching the writer routinely carries four by-design non-numeric keys: `reserves_date` (str), `trading_day` (str), `nbr_fytd_cross_check` (str), and `commodity_change_pct` (dict). The warning would fire on each of them every aggregate run — multiple identical warnings per day, indefinitely — which would desensitize the operator to the very signal the warning was trying to create.

**Lesson:** When adding a "warn on shape X" observability rule, first audit the codebase for routine, by-design occurrences of shape X. If any exist, build an allow-list before shipping the warning. Otherwise the warning becomes the noise it's trying to detect, and the next real bug lands inside a stream of identical false positives.

**Prevention:** Before adding any `logger.warning` at a filter boundary, grep the upstream call sites for assignments to the filtered structure (in this case, `grep -nE 'data\["[a-z_]+"\]\s*=\s*[^0-9a-z_.]' aggregate_latest.py`). For each routine non-numeric assignment found, either (a) move it out of the filtered dict, or (b) add the key to an allow-list and skip silently.

**Hotfix:** PR #33 (`77a36b7`) — added `_KNOWN_NON_HISTORY_KEYS` frozenset checked before the warning. Tests cover `str` warns, `None` warns, allow-list silent, and allow-list silent even with unexpected shape.

**Cross-references:** PR #33 (`77a36b7`), PR #32 (`958a00e`) — Entry 4 caveat documents the allow-list requirement.

## 2026-05-28 — BB MEI bulletin page numbering shifts edition-to-edition

**Trigger:** Investigating where corridor rates (Repo / SDF / SLF) live in the BB Monthly Economic Indicators (MEI) bulletin PDF, for PR #30.

**What went wrong:** The existing `policy_rate_slf_sdf` alternate config (and `call_money_rate` alternate config too) pointed at "page 7 of the doc, first table". But page 7 of the April 2026 MEI bulletin is "Reserve money developments". The actual financial-sector-prices table containing Repo/SDF/SLF/Call Money rates is on **page 10**. Likely the bulletin was reorganized between editions, and nobody updated the alternate hints.

**Lesson:** Page numbers in `config/sources-v3.json` `task` hints decay silently. Whenever using a PDF page-number hint, verify against the latest published PDF — don't trust 12-month-old config text.

**Prevention:** Before touching a `pdf_table_*` parser config, fetch the latest PDF via `fetchers.pdf_fetcher.fetch_pdf` and `pdfplumber.open(...).pages[N-1].extract_text()` to confirm the page contents match the task hint.

**Hotfix:** PR #30 — corridor split routed to page 10. Two remaining wrong page-7 hints in `call_money_rate.alternate.task` should also be fixed in a follow-up.

**Cross-references:** PR #30 (`121969b`).

## 2026-05-28 — pdfplumber multi-line headers contain literal `\n`

**Trigger:** Writing the `pdf_table_column_latest` parser strategy in PR #30.

**What went wrong:** Page 10 of the MEI bulletin renders the header cell "Policy rate (repo)" across two lines (the parenthetical wraps). When pdfplumber extracts the table cell, the literal string is `"Policy rate\n(repo)"` — embedded newline. A naive case-insensitive equality match against the instruction string `"Policy rate (repo)"` fails.

**Lesson:** pdfplumber's table extraction preserves visual line breaks in multi-line header cells as `\n` characters. Any column-by-header matcher must whitespace-normalize before comparing.

**Prevention:** Always normalize via `re.sub(r"\s+", " ", text).strip().lower()` (or equivalent) before string comparison in PDF header matchers.

**Hotfix:** PR #30 added a `_normalize_header` helper in `parsers/pdf_table_column_latest.py`.

**Cross-references:** PR #30 (`121969b`).

## 2026-05-28 — `html_footer_ticker` unreliable for rotating tickers

**Trigger:** Investigating why `policy_rate_slf_sdf` had been returning a stable value of `10.00` for 15 consecutive days (May 14–28).

**What went wrong:** The `html_footer_ticker` parser uses a regex to find a label (e.g. "Policy Rate") in the rendered HTML text and grab the numeric token immediately after. But BB's homepage footer is a **rotating ticker** that cycles through multiple unlabelled rates. The scraper was reliably grabbing one rate every day (turned out to be the Repo) but couldn't distinguish Repo from SDF from SLF from anything else on the ticker — they all just appear as numbers in sequence.

**Lesson:** The `html_footer_ticker` pattern works for pages where each label has a stable adjacent value. It fails silently on rotating tickers / marquee elements / any DOM where multiple unlabelled numbers share the same parent. Symptom: value stays plausible but is actually wrong.

**Prevention:** Before using `html_footer_ticker` on a new BB page, inspect the page's DOM with a browser — if the labels and values are in a `<marquee>` or any JS-cycled container, pick a different source (likely the BB MEI bulletin PDF instead).

**Hotfix:** PR #30 — retired `policy_rate_slf_sdf` (homepage ticker scrape) in favor of 3 PDF-table-sourced explicit corridor metrics.

**Cross-references:** PR #30 (`121969b`).

## 2026-05-28 — Supabase writer drops non-scalars without warning

**Trigger:** Investigating why `call_money_rate` had 0 rows in Supabase `metric_history` despite the scraper being configured `cadence: daily` for ~6+ months.

**What went wrong:** The Supabase writer's `_rows_from_data` scalar gate (`utils/supabase_writer.py`) filters to numeric values only — `dict` and other non-numeric types are silently dropped. The `html_call_money` parser legitimately returns a dict of 4 tenors `{"1D": 9.50, "7D": 9.75, "14D": 10.10, "90D": 10.50}`. The aggregator's `_flatten_dict_indicators` knows how to fan dicts into per-key scalars, but only handles `dse_sector_heat` (the precedent pattern). `call_money_rate` was never added to the flattener. So every day for months: parser succeeds → aggregator passes the dict through → writer skips it → Supabase row count stayed at 0. **Zero log lines anywhere flagged the drop.**

**Lesson:** Silent filtering is the worst kind of bug — there's no signal that anything is wrong until someone notices the absence. The aggregator's flatten step is the right pattern but it scales by manual per-indicator registration; observability at the writer boundary is the missing complement. **Caveat:** a naive warn-on-all-non-scalars warning fires daily on by-design metadata keys (e.g. `reserves_date`, `trading_day`, `nbr_fytd_cross_check`, `commodity_change_pct`) — observability that warns on every clean run is noise, not signal. Combine the warning with a small allow-list of known-metadata keys.

**Prevention:** A `logger.warning("supabase_writer: dropping non-scalar value for metric_id=%s (type=%s)", metric_id, type(value).__name__)` at the filter boundary, gated on an allow-list of known-metadata keys, would have caught this on the day the parser shipped without spamming alerts daily.

**Hotfix:** PR #31 (`556ba05`) — extended `_flatten_dict_indicators` to handle `call_money_rate`. Follow-up observability via PR #33 (`logger.warning` at writer boundary + allow-list).

**Cross-references:** PR #31 (`556ba05`), PR #33 (observability follow-up), AGENTS.md landmine 8 (BRIEF_ALIASES auto-promotion).

## 2026-05-25 — NBR FYTD news scrapers retired due to time-window drift

**Trigger:** Discord `#econdelta-alerts` channel firing 9 consecutive aggregate-rejected alerts in one day (2026-05-25 at 13:00, 14:00, and 16:31 timer windows).

**What went wrong:** `nbr_fytd_collected_tbs` and `nbr_fytd_collected_dailystar` were two news-scraper sources for the NBR's fiscal-year-to-date tax collection figure. Their URLs were tag-listing pages (`/tags/nbr`) — every day they'd pick up whichever was the most recent NBR-tagged article. The articles' time windows drifted: prior days were citing 10-month-FYTD figures (~287,000 cr), but on 2026-05-25 the most recent Daily Star article cited a 9-month-FYTD figure (~203,000 cr). Same indicator label, two different real-world time windows. The aggregator's cross-check (TBS vs Daily Star average against a tolerance band) flapped because TBS had silently stopped reporting the cumulative figure too.

**Lesson:** Tag-listing news pages aren't a stable source for cumulative figures. The same label can refer to different real-world quantities depending on which time window the article happens to cover.

**Prevention:** For any FYTD/YoY/MoM cumulative figure, source from a publication that reports the value with explicit window labelling (e.g. the BB MEI bulletin's "Government tax revenue collections" table, which always reports the latest month's value with an explicit "as-of" date), not a news article's free-text summary.

**Hotfix:** PR #28 (`5f07c45`) — retired both news scrapers, aliased `nbr_fytd_collected_cr` directly to `tax_revenue` (BB PDF source, deterministic, stable at 287,862.59 crore for 10+ consecutive days).

**Cross-references:** PR #28 (`5f07c45`), AGENTS.md landmine 4 (NBR FYTD canonical = tax_revenue).
