# Handoff — EconDelta fixes from the 2026-07-04 ecosystem review

**Created:** 2026-07-04 (BDT) by a Fable 5 orchestrator session in the-brief repo.
**Execute with:** Claude Opus 4.8 (`/model opus`), fresh session opened in THIS repo (`~/Projects/clauding-lab/econdelta`). This brief is self-contained — the executing session has NO memory of the review session. Read AGENTS.md (32 landmines), VISION.md, AGENT_LEARNINGS.md first, as always.
**Provenance:** 25 review agents (Opus 4.8) over the repo, live Supabase (project `ssbliukchgibjcjohibi`), and read-only SSH to ExonVPS. Every P0/P1 claim below was adversarially re-verified by an independent agent; verdicts are noted. Nothing was modified anywhere — the repo, the VPS, and Supabase are exactly as the review found them.

## Approval gate (re-confirm with Adnan before acting)

Adnan approved this plan in principle on 2026-07-04 but deferred execution. Before touching anything below, restate and get a fresh yes for:

1. **ExonVPS writes** (`ssh adnan-local@103.187.23.22`, hostname `local.clauding-lab.com`): CA-bundle fix, `git pull` after merges, installing/enabling new systemd units, running backfills on the box.
2. **Mac writes**: same CA-bundle fix in the laptop venv (landmine 13 launchd backup).
3. **Supabase production data**: any DELETE/correction of poisoned rows (e.g. `banking_sector_crar=1.56`), and DDL (new column/view) — DDL goes through **Adnan's SQL editor only** (no programmatic path; same rule as the-brief).
4. Repo changes ship branch → PR → Adnan's merge approval, per VISION.md.

Never read `/etc/econdelta.env` contents or credential files. One command per ssh. `mv` to scratch instead of `rm -rf`. No force-push, no `--no-verify`.

**SSH identity:** `exonhost` (the alias in AGENTS.md commands) = `adnan-local@103.187.23.22`; the box hostname now reads `local.clauding-lab.com`. Use `ssh exonhost` if the alias resolves, else `ssh adnan-local@103.187.23.22`.

**Verification credentials (for the fresh SELECTs this brief keeps demanding):** use the anon key — `NEXT_PUBLIC_SUPABASE_URL` + `NEXT_PUBLIC_SUPABASE_ANON_KEY` in `/Users/adnanrashid/Projects/clauding-lab/the-brief/.env.local` (same Supabase project), or the session's Supabase MCP tools (SELECT only). Anon-read is repo-confirmed for `metric_history` (migration `0005`); for `metric_history_monthly` it was observed live but is NOT in a committed migration — if anon SELECT on the monthly table 403s, hand the query to Adnan's SQL editor. Never pull the service-role key off the box.

## The systemic diagnosis (why these bugs share a shape)

Every failure below is "success measured at the wrong layer": all 16 systemd timers fire on schedule (verified 2026-07-04), logs print `OK` / `upserted 128 rows`, run_logs rows say `ok` — while the data silently freezes or lies. `upsert_metric_history` returns `len(batch)` of what it POSTed (`utils/supabase_writer.py:201`, gated only on 2xx at `:197`), never what landed. There is **no freshness monitoring anywhere** (grep for freshness/sentinel/staleness returns zero). Phase E2 exists to kill this class, not just these instances.

---

## Phase E1 — stop the bleeding (do first)

### E1.1 — P0: ~22 core indicators frozen since 2026-06-05 (source_as_of static-row bug) — VERIFIED CONFIRMED

**Symptom:** the whole inflation family (`point_to_point_inflation`, `general/food/non_food_inflation`), `deposits_of_the_system`, `currency_outside_bank`, `money_multiplier`, `deposits_held_with_bb_crr`, `excess_liquid_asset_total_minimum`, `monthly_import_lc_opening/settlement`, `categorywise_export`, `categorywise_fy_import_breakdown`, `remittance_by_country`, `debt_domestic/external_stock_cr` + Brief aliases (`macro_cpi_headline/food/nonfood`, `fiscal_bank/foreign/govt_borrow_trn`) — 22 ids verified — have `max(as_of)=2026-06-05`, zero rows with `ingested_at > 2026-06-06`. Meanwhile parse-systemd.log shows parse_all producing their values EVERY day (`money_multiplier=5.37`, `debt_domestic_stock_cr=1247151`, provenance=llm_extracted), and aggregate logs "upserted 128 rows" daily.

**Mechanism (verified):** the source_as_of recovery commits `cd4e580` (#65), `179731a` (#64), `03096c6` (2026-06-03/04) make `aggregate_latest.py` write each affected indicator to its recovered reporting-period `as_of` (e.g. `debt_domestic_stock_cr` → `as_of=2025-12-31`). The upsert (`ON CONFLICT (metric_id, as_of) DO UPDATE`) lands on the SAME static row every day; `ingested_at` is not a posted column so it never updates. Values are updated in place — not lost — but no fresh `as_of`/`ingested_at` ever appears.

**Design nuance — do not "fix" this naively.** The cross-project contract review concluded the vintage rule is CORRECT: `as_of` SHOULD be the source's reporting vintage and SHOULD stall when the source hasn't republished (the pre-June behavior of stamping `as_of=today` on a monthly figure was the actual lie — `deposits_of_the_system` wrote the identical value under advancing daily dates). So the work is:

1. **Investigate whether the recovered vintage advances when the source republishes.** BB publishes MEI monthly; if April/May/June 2026 MEI issues exist but the recovered `source_as_of` still says an old period, the fetch is retrieving a stale PDF or the recovery regex is locking onto an old date (see landmine 29 — `finditer` latest-date rule). That's the real bug. Check `data/_pdfs/`/fetch logs for which MEI issue is being fetched.
2. **Make `ingested_at` bump on merge-upsert** (add it to the posted columns in `_rows_from_data`, `utils/supabase_writer.py:115-120`) so write-liveness is observable even when `as_of` legitimately stalls.
3. **Decide the consumer-facing read rule** with Adnan: consumers using `max(as_of)` on these ids now see the last pre-recovery daily-stamped row (2026-06-05), not the recovered vintage rows. Options: clean up the legacy daily-stamped rows for monthly-cadence ids (Supabase data change — approval gate), or document that consumers must read latest-by-`ingested_at` for vintage-stamped ids — **but note `db/schema.sql:98-100` explicitly says "ingested_at … Diagnostics only — consumers should order by `as_of`"**, so the latter option means amending that schema comment too; Adnan decides. The freshness view in E3 makes this moot for new consumers.
4. **Add a post-upsert landed check** (see E2.2) so a shrunk/no-op batch fails loudly.
5. **Add a regression test**: an indicator with recovered source_as_of still produces an observable "fresh write" signal each run.

### E1.2 — DSE feed dead since 2026-06-11: incomplete TLS chain — VERIFIED CONFIRMED

**Root cause (verified by openssl):** DSE renewed its cert ~Jun 11. `openssl s_client` to `www.dse.com.bd:443` → leaf `CN=*.dsebd.org`, issuer `Sectigo Public Server Authentication CA DV R36`, **`Verify return code: 21 (unable to verify the first certificate)`** — the server doesn't send the intermediate. `www.bb.org.bd` verifies clean (code 0), box `ca-certificates` healthy, VPS checkout at origin/main — external cause, not code or network block. Python requests/urllib3 doesn't AIA-chase intermediates like browsers.

- `scrapers/dse_market.py` (index: `dsex`, `ds30`, `dses`, `turnover_crore`, `advancing/declining`) — frozen at 2026-06-11 (dsex=5516.82). Its run_logs show `ok` on non-trading days ("skipping") but writes nothing on trading days.
- `scrapers/dse_dayend.py` / `scripts/backfill_dse_dayend.py` (30 `dse_close_*` tickers) — frozen 2026-06-10; run_logs 30/30 `fail`, `SSLError ... CERTIFICATE_VERIFY_FAILED` against bare `dsebd.org` (note: dayend hits `dsebd.org`, market hits `www.dse.com.bd` — both broken).
- The Mac launchd backup (landmine 13) runs the same code against the same host → identical failure; `data/dse_market/` on the Mac holds only 204-byte non-trading stubs since mid-June. The backup is blind to host-side failures.

**Fix:** fetch the Sectigo intermediate via the leaf's AIA URL and append it to the CA bundle used by `utils/http_client.py` (or pin the full chain for the DSE hosts). Do NOT set `verify=False`. **First confirm both fetch paths share that bundle:** `dse_market` fetches via `utils/http_client`, but `scripts/backfill_dse_dayend.py` may make its own `requests` calls to `dsebd.org` — if so, one bundle change won't cover both; route the backfill through the same client or apply the fix at the `requests`/certifi layer both use. Apply on ExonVPS **and** the Mac venv (approval gate items 1+2). Then, on the VPS (BD egress required — dsebd.org firewalls non-BD IPs): one bounded real run of `dse_market`, and `python -m scripts.backfill_dse_dayend` with a window covering 2026-06-11 → now (upserts idempotent). Verify via SELECT that `dsex` and `dse_close_*` advance to the latest trading day. Downstream: The Brief's DSEX chart and DS30 movers self-heal after its next publish.

### E1.3 — `banking_sector_crar = 1.56` is fabricated — VERIFIED CONFIRMED

Single row, `as_of=2025-09-30`, value 1.56 (real BD system CRAR is ~10-13%; 1.56 implies systemic insolvency). Mechanism verified: `parse-systemd.log` every run — deterministic component parse fails (`component ... not found in PDF`) → LLM fallback emits 1.56 with `provenance=llm_extracted`, which passes `_is_bad_snapshot` (`aggregate_latest.py:~202-210` treats only needs_review/extract_failed/None/0 as bad) and Opus review (only checks vs recent history — a consistently wrong value reads as "consistent"). Both The Brief and YieldScope display this number.

**Fix:** (a) add a `valid_range` guard for `banking_sector_crar` (e.g. reject outside 2–30%) so a misextract drops to needs_review, never persists — mirror the landmine-27 MetricSpec pattern; (b) investigate the QFSAR deterministic parser (layout likely changed) and whether BB has published a newer QFSAR (press reports suggest a newer, possibly negative CRAR print — verify against BB primary source, do NOT trust press numbers without the PDF); (c) with Adnan's sign-off, correct/remove the poisoned 1.56 row in Supabase (approval gate item 3). NPL (`gross_npl_ratio`) is fine — it advances via a media-approved override (32.26 @ 2026-03-31).

### E1.4 — `policy_rate_sdf` writes 7.50 daily while the real SDF floor is 8.50 — INVESTIGATE-ONLY, NOT independently verified

Observed twice on the live YieldScope surface + once by direct Supabase read (`policy_rate_sdf = 7.5`, fresh `as_of=2026-07-04`), while `policy_rate_repo=10.00` and `policy_rate_slf=11.50` are correct — but unlike its siblings this got NO adversarial verification pass and no root-cause trace. Start: (1) re-confirm via anon SELECT that the latest `policy_rate_sdf` row still says 7.5; (2) find its producer — grep `config/sources-v3.json` for `policy_rate_sdf` and follow the indicator's fetch/parse chain; (3) determine where 7.5 comes from (old carry-forward re-stamped? wrong table cell? BB changed the corridor and 8.50 is the stale memory? — check BB's current MPS corridor before assuming which value is right); (4) fix, and add a corridor coherence check (SDF < repo < SLF, spacing sanity). The 8.50 reference value comes from the live The Brief issue 156 and YieldScope's SLF/repo cross-check — treat it as strong prior, not gospel. YieldScope renders the 7.5 unbadged; The Brief is unaffected only because it hardcodes the corridor (its own bug, fixed in the-brief handoff).

### E1.5 — World Bank pink sheet: 10/10 green runs writing nothing since 2026-06-01 — VERIFIED CONFIRMED

`run_logs` `world_bank_pink_sheet`: last 10 runs all `status=ok, exit_code=0`; `lng_price_usd_mmbtu`, `palm_oil_price_usd_mt`, `wheat_price_usd_mt` each have exactly 1 row, `as_of=2025-12-31`, `ingested_at=2026-06-01`. Landmine 22/23 class. Determine whether (a) the scraper on the box predates PRs #54/#55 (deploy drift — check `git -C ~/econdelta log`), (b) WB genuinely hasn't published newer months (then log "no new month" explicitly, don't report ok-and-silent), or (c) the write still no-ops. Add the read-back verification (E2.2) regardless.

### E1.6 — dse_dayend has ZERO alerting; aggregate swallows total write failure — VERIFIED CONFIRMED

- `scrapers/dse_dayend.py` + `scripts/backfill_dse_dayend.py` import no `utils.notifier` — the ONLY scraper without it (siblings all call `notify("error", ...)`: bb_forex.py:412, commodity_prices.py:146, world_bank_pink_sheet.py:299, bb_auction.py:611, dse_market.py:268, etc.). All-fail → `return 1` → run_logs `fail` → nobody pinged. No `OnFailure=` in any deploy/*.service. This is exactly the 24-silent-day path. Fix: `notify("error", ...)` when `all_rows` is empty or below a floor (~25/30 tickers), and on `SupabaseWriteError` before re-raising. Test: all-fetch-fail asserts BOTH exit 1 AND notify called.
- `aggregate_latest.py:1088-1091`: `except SupabaseWriteError` → `logger.warning` → falls through to `return 0` → run_logs `ok`. A rotated key or PostgREST outage = consumers silently serve yesterday, no signal. Fix: `notify("error", ...)` on that path (keep continuing with local archive — the fallback is right, the silence is not).

---

## Phase E2 — the reliability kit (kills the whole silent class)

### E2.1 — Freshness sentinel (the central deliverable; full design agreed)

New oneshot `econdelta-sentinel.{service,timer}`, daily ~13:30 BDT (07:30 UTC, after aggregate at 13:00 BDT grades the day). **Landmine 19:** add it to `install.sh`'s hardcoded `TIMERS=()` array (`:95-100`) in the same PR or it will be copied-but-never-enabled.

- **Reads** (service-role via existing `utils/supabase_reader.py`): per metric_id `max(as_of)` + `max(ingested_at)` across **both** `metric_history` AND `metric_history_monthly` (a sentinel that skips the monthly table scores it absent, not stale).
- **Cadence + grace:** join metric_id→cadence from `config/sources-v3.json` (+ `BRIEF_ALIASES` targets). Grace: daily → 2 BD trading days (via `utils/calendar.py` + `config/holidays_2026.json`); weekly → 10d; monthly → 45d; quarterly → 165d; fiscal_year → 400d. Breach if `today − max(as_of) > grace`.
- **Output:** ONE Discord digest to `#econdelta-alerts` via `utils/notifier.py` listing breaches (metric | cadence | last as_of | age); weekly "all N fresh" heartbeat so silence is never ambiguous.
- **Dead-man's-switch:** sentinel writes `run_logs (source='freshness_sentinel')` via `log_run_start/end`. The Brief's Hetzner heartbeat (built in the-brief handoff, B-phase) checks `get_recent_run_ok('freshness_sentinel', within_hours=26)` off-box and alerts if the sentinel itself goes quiet. Zero new plumbing.
- Retro-test: it must fire on the four current clusters (DSE @ day ~2, pink-sheet within grace, CRAR at quarterly grace, 06-05 cluster per its true cadence).

### E2.2 — Post-write landed-count invariant (day-1 catch)

After the aggregate upsert (and inside the direct writers), re-query cheaply — `count(*) where ingested_at > run_start` (or `Prefer: count=exact` HEAD / `return=representation`) — and compare against the intended batch. Mismatch → `notify("error")`. This is the enforced guard landmine 22 never got, and it catches both the pink-sheet class and the E1.1 class the same run. Requires E1.1's `ingested_at` bump to be meaningful.

### E2.3 — Deploy drift: one ordered git-pull timer

Deploy is a manual `ssh git pull` (AGENTS.md:45,56) — merge ≠ deploy, the-brief landmine-21 class. Do NOT add per-unit `ExecStartPre=git pull` — 16 units would pull concurrently around 05:00 BDT and could swap code mid-cascade (fetch on old code, parse on new). Add ONE `econdelta-gitpull` oneshot timer ~04:50 BDT (`git pull --ff-only` + conditional `daemon-reload`, HEAD logged), before `econdelta-fetch` (05:00). Interim: fold a HEAD-vs-origin behind-check into the sentinel digest. (Landmine 5: schedule the enable outside the catch-up window or set `Persistent=false` first.) **Fire times quoted here are from the review's `list-timers` snapshot — read the actual `OnCalendar=` lines in `deploy/econdelta-*.timer` before picking the slot** (units are in UTC; the daily cascade runs ~05:00–05:15 BDT with aggregate ~13:00 BDT).

### E2.4 — Off-box export of irreplaceable history

`data/` is entirely git-ignored and exists only on ExonVPS; `metric_history_monthly`'s hand-verified fiscal backfill (landmine 32) and all LLM-extracted history are NOT re-scrapable; Supabase is the single off-box copy. Add a weekly export (pg_dump-style via PostgREST or the reader) of `metric_history_monthly` + the LLM/static tier landing OFF the box (Hetzner, where the-brief's export job will live, or a git-tracked snapshot). The re-scrapable daily market series are lower priority.

### E2.5 — Zero-rows ≠ success (deterministic floor)

`fetch_all.py:113-133` and `parse_all.py:106-226` return 0 regardless of per-indicator failure counts; opus_review is fail-open on operational errors (`opus_review.py:128-143`). Add a deterministic floor: alert when parse yields fewer than N snapshots or fetch failures exceed a threshold — before the LLM review, independent of it.

---

## Phase E3 — contract + PWA honesty (same repo)

### E3.1 — Contract in the database (DDL via Adnan's SQL editor — approval gate 3)

- Add `grace_days` to `metric_definitions` / `metric_definitions_monthly`.
- Create view `v_metric_freshness (metric_id, latest_as_of, cadence, grace_days, age_days, is_fresh)` over both tables. All three consumers (The Brief, YieldScope, EconDelta PWA) read THIS instead of hand-rolling staleness — the one place the gate lives.
- Canonical rule to document in `docs/data-contract.md` (version it, cite it from both consumer repos' AGENTS.md): **`as_of` = the source's reporting vintage; never advance without republication; freshness = `as_of ≥ today − grace(cadence)`.**
- Dedupe: mark legacy ids (`dse_dsex_close`, `policy_rate_slf_sdf`, `nbr_fytd_collected_tbs/dailystar`, `bb_gross_reserves`, `comm_lng_jkm`) via an `alias_of`/deprecated marker (or prune rows with sign-off); note `debt_gdp_ratio` max(as_of)=2031-12-31 is an IMF projection — exclude future `as_of` from "latest" reads or split projections to a separate id. RLS note: anon-read on `metric_history` is repo-confirmed (migration `0005`) — **AGENTS.md landmine 18 is superseded for the daily table; update it.** Anon-read on `metric_history_monthly` + auction tables and the "two duplicate anon policies" observation came from a live `pg_policies` read with no committed migration behind them — re-run `select tablename, policyname, roles, cmd from pg_policies where tablename in ('metric_history','metric_history_monthly','auction_calendar','auction_results')` (Adnan's SQL editor if anon can't read pg_policies) before rewriting the landmine or deduping.
- 12 config ids have never produced a row (budget_opex/adpex, non_tax/non_nbr/total_revenue, tax/rev_gdp_ratio, fx_buy_sale_from_market, nbr_vat/it/customs, ways_means): give each a real source or retire it. `non_nbr_tax_revenue` still contained a literal `TODO_VPS_FILL_FY26_NON_NBR_BUDGET_CRORE` anchor. **Both lists are a 2026-07-04 snapshot — re-grep `config/sources-v3.json` and re-run the zero-row SELECT before acting.**

### E3.2 — PWA: stop discarding the truth it already fetches

The RPC `get_latest_dashboard` returns per-metric `{value, as_of}`; `pwa/lib/supabase-client.js:236-239` keeps only `.value` — so 23-day-old DSEX and 185-day-old LNG render dateless under a live "updated 4m ago" stamp. Fixes, in order of value:
1. Thread `as_of` into `flatValues`; render per-ticker vintage + staleness pill (amber > cadence, red > 2× cadence — or read `v_metric_freshness` once E3.1 lands).
2. Kill the hardcoded mock chrome: masthead `Vol. 1, No. 122 / 2026-05-02 SAT / 10:35 UTC` (`pwa/components.jsx:320-322`), sidebar `all sources OK / last sync 2026-05-02` (`:133-134`, tooltip `:100`) — drive from live data.
3. `relTime` frozen anchor `2026-05-02T10:35:00Z` (`components.jsx:27-33`) makes every source read "just now" — pass the live ticking `now`.
4. Relabel "updated <time>" as "snapshot fetched" + show oldest-metric age.
5. Add a "Stale metrics" table on the Runs page (metric, last as_of, days since change, expected cadence, writing source) — run-health ≠ data-freshness; this is the surface that would have caught every silent freeze.
6. Backfill `runs.jsx` CADENCES / `components.jsx` SOURCE_LABELS for the 7 newer sources; fix the sidebar "Sources: 4" badge; treat long-`running` (media_screen) as amber. Hide the three NBR "—" tickers with no data (`supabase-client.js:248` intent).

---

## Sequencing and verification discipline

E1.2 (TLS) and E1.6 (alerting) are independent quick wins — start there while investigating E1.1. E1.1's read-rule decision and all DDL need Adnan. After each fix: verify the write LANDED via a fresh SELECT (never trust "wrote N rows" — landmine 22), run `pytest` + `ruff check .` full-gate (no piping through tail/grep), and follow the pre-merge smoke list in AGENTS.md. Update AGENT_LEARNINGS.md with each incident per the house rulebook (the source_as_of freeze, the TLS chain, the fabricated CRAR are all rulebook-worthy). Related consumer-side handoffs (absolute paths — they live in sibling repos this session won't otherwise see): `/Users/adnanrashid/Projects/clauding-lab/the-brief/docs/handoff/2026-07-04-review-fixes.md`, `/Users/adnanrashid/Projects/clauding-lab/YieldScope/docs/handoff/2026-07-04-review-fixes.md`. The Brief's own `handoff.md` (repo root, 2026-05-31) is now partially superseded: F4's "build a daily DS30 job" already exists (`scrapers/dse_dayend.py` + timer) — the blocker was this TLS failure; F7 (fiscal monthly deepening) remains valid and is referenced by E2.4/E3.1.
