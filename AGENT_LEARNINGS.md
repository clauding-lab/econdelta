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
