# AGENTS.md — EconDelta

Operational rules for AI coding agents (Claude Code, Cursor, Codex CLI, etc.) working in this repo. Read this in full before making any code change.

## What this project is

EconDelta is the **data layer** for Bangladesh economic indicators. ~60 series scraped daily from Bangladesh Bank, BBS, NBR, DSE, DAM, and news outlets; parsed (deterministic regex / table extractors + Claude LLM fallback); written to a canonical `data/latest.json` snapshot AND a row-per-indicator-per-day in Supabase `metric_history`. Python 3.11+, deployed via systemd timers on ExonVPS (Exonhost BDIX, Dhaka). A Mac-laptop launchd path is a belt-and-suspenders backup for two flaky scrapers. Downstream apps (The Brief, Mission Control) read from Supabase — they do NOT depend on EconDelta's Python code. The dashboard PWA at `econdelta.clauding-lab.com` (Next.js, GitHub Pages) is the human surface.

Owner: solo dev (Adnan, Bangladesh, UTC+6). Vibe-coded — Adnan directs AI agents, does not hand-write code himself. All explanations, summaries, and prose should be in **plain English with technical terms briefly explained**, never assume Adnan reads code.

## Repository structure

```
aggregate_latest.py   top-level aggregator: merges scrape outputs → data/latest.json + Supabase
fetch_all.py          top-level fetcher orchestrator (Stage 1 — pull raw artifacts)
parse_all.py          top-level parser orchestrator (Stage 2 — LLM extraction)
scrapers/             one-shot scrapers (bb_forex, bb_forex_captcha, commodity_prices, dse_market)
fetchers/             reusable fetchers — html, pdf (+ stealth Playwright variant), news article discovery
parsers/              extractors — html_*, pdf_*, hybrid, dam_ticker, dse_sector_heat, registry
claude_max/           Claude Max OAuth client + validators + prompt files
utils/                anomaly, calendar, http_client, notifier (Discord), opus_review, schema, supabase_writer
config/               sources-v3.json (indicator registry), holidays_2026.json, thresholds.json
data/                 per-indicator <date>.json + _html/ + _pdfs/ + latest.json + archive/
db/                   schema.sql (canonical reference snapshot) + README
supabase/             config.toml + migrations/ (applied via `supabase db query -f`; see db/README)
deploy/               systemd .service + .timer units + install.sh + logrotate.conf
laptop/               Mac launchd .plist files + run-and-sync.sh
pwa/                  Next.js dashboard (deploys to GitHub Pages)
scripts/              build_catalog.py and other one-off ops scripts
docs/                 data-contract, indicator-catalog (autogen), superpowers/specs|plans/
tests/                pytest suite (~358 tests)
```

## Build, Test, Run

| Goal | Command |
|---|---|
| Dev loop (run one scraper) | `python -m scrapers.bb_forex` |
| Dev loop (run aggregator) | `python -m aggregate_latest` |
| Unit + integration tests | `pytest` |
| Tests with coverage | `pytest --cov=utils --cov=scrapers --cov=fetchers --cov=parsers --cov=claude_max --cov-report=term-missing` |
| Lint | `ruff check .` |
| Format | `ruff format .` |
| Regenerate indicator catalog | `python scripts/build_catalog.py > docs/indicator-catalog.md` |
| VPS deploy (backend) | `ssh exonhost 'cd ~/econdelta && git pull origin main'` |
| VPS install/refresh timers | `bash deploy/install.sh` (run on VPS as sudo-capable user) |
| PWA deploy | automatic via `.github/workflows/pwa-deploy.yml` on `pwa/**` changes |

There is no backend CI — the only GH Action is `pwa-deploy.yml` and it ONLY fires when `pwa/**` or the workflow itself changes. Backend changes (`scrapers/`, `aggregate_latest.py`, `config/sources-v3.json`, etc.) DO NOT run tests in CI. Run `pytest` locally before merging anything outside `pwa/`.

## Release flow

No version tags. Releases are continuous:

- **PWA** — `git push origin main` with any change under `pwa/**` triggers `pwa-deploy.yml` which deploys to `econdelta.clauding-lab.com`. Build step rewrites `pwa/sw.js` CACHE_NAME + stamps `__BUILD_VERSION__` into `pwa/index.html`.
- **Backend** — SSH to ExonVPS, `git pull origin main`. Systemd timers pick up the new code on next fire. No restart of timers needed unless `deploy/*.service` or `deploy/*.timer` files changed (then `sudo systemctl daemon-reload` + restart the affected timer).
- **Brief consumer** (separate repo, runs on Hetzner) — pulls `data/latest.json` via 5-min cron. The data contract — Supabase `metric_history` schema + indicator IDs in `config/sources-v3.json` — is the stable interface. Internal scraper/parser shapes are implementation details.

Pre-merge smoke list for backend changes:
1. `pytest` green locally
2. `ruff check .` clean
3. If `config/sources-v3.json` changed → `python scripts/build_catalog.py > docs/indicator-catalog.md` and commit the regenerated doc

## Coding style

- **Python:** ruff (`ruff check .` + `ruff format .`). `line-length = 100`, `target-version = "py311"`, selects E/F/W/I, ignores E501. PEP-8 with type hints on signatures.
- **JS/JSX (PWA):** no auto-formatter wired up; match the existing style in `pwa/`. Plain JS, no TypeScript. JSDoc for non-trivial functions.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `polish:`, etc.) with optional scope like `feat(bb_forex):`. Imperative mood. **No `Co-Authored-By: Claude` lines** — attribution is disabled globally; do not re-add.
- **Files:** keep modules focused; ~400 lines typical, 800 max.

## Known landmines (read before touching these areas)

1. **claude CLI version pin (2.1.104).** CLI versions 2.1.105+ regressed non-interactive OAuth refresh — every cron-fired `parse_all.py` call returns HTTP 401 from Anthropic. ExonVPS is pinned at 2.1.104. **Do not upgrade `claude` on ExonVPS without re-verifying the cron path works on the new version.** The Brief on Hetzner is also at 2.1.104 for the same reason. Fix-of-the-fix: `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` is stored in `/etc/econdelta.env` (1-year token) — that bypasses the refresh dance entirely. Don't break this env var.

2. **NEVER use `ANTHROPIC_API_KEY` anywhere.** That env var triggers pay-per-call API billing. EconDelta is on the Max subscription only. Use OAuth via `.credentials.json` or `CLAUDE_CODE_OAUTH_TOKEN`. Both `claude_max/max_client.py` and any cron-callable script must respect this.

3. **BB serves image-CAPTCHA wall to flagged IPs.** `scrapers/bb_forex_captcha.py` handles the bypass via `claude -p --model claude-haiku-4-5 "<prompt> @<imagepath>"` (the `@<path>` is part of the prompt string, NOT a separate argv). Don't rewrite this as a `--file` flag — that doesn't trigger vision-mode attachment. 3-attempt loop with 60s timeout. If you raise the attempt count, also raise the per-run wall-clock budget in the systemd `econdelta-forex.service` unit.

4. **NBR FYTD canonical source is `tax_revenue` (BB PDF), not news scrapes.** The legacy `nbr_fytd_collected_tbs` and `nbr_fytd_collected_dailystar` scrapers were retired 2026-05-25 — both hit tag-listing pages whose latest article describes different fiscal-year time windows, so the cross-check flapped. Don't re-add them. If you need a second NBR corroborator, pin a specific URL or extract the time-window explicitly.

5. **`Persistent=true` footgun on systemd timers.** Changing `OnCalendar=` + `daemon-reload` + `systemctl start <timer>` causes an immediate catch-up fire if today's instance of the new schedule has already passed in local time. Tolerable but costs ~$1 of Claude calls per catch-up. To avoid: temporarily set `Persistent=false`, or reschedule during the safe window when today's slot is still in the future.

6. **Aggregate restart cap = 3.** Systemd's `Restart=on-failure` with `StartLimitBurst=3` means after 3 consecutive failures within the timer window, you'll see `Start request repeated too quickly` and the timer gives up. The 14:00 retry timer is the second chance. If both fail, `latest.json` carries yesterday's data forward (graceful degradation by design — don't "fix" this).

7. **Opus aggregate review can reject the day's data.** `utils/opus_review.py` compares proposed `data` against the last 5 days of archived `latest.json`. On reject, the run exits non-zero, fires a Discord alert at `#econdelta-alerts`, and keeps yesterday's `latest.json`. This is the safety net — DO NOT bypass it without checking what it caught. Set `ECONDELTA_SKIP_OPUS_REVIEW=1` only for emergency debugging.

8. **Aggregate auto-promotes some indicators via `BRIEF_ALIASES` and `BRIEF_CONVERSIONS` in `aggregate_latest.py`.** Adding a new scraper that should appear under a `macro_*`, `fiscal_*`, `banking_*`, or `food_*` key for The Brief requires also adding to one of those dicts. After editing → regenerate `docs/indicator-catalog.md` via `scripts/build_catalog.py`.

9. **Brief tb_* tables (`tb_macro_*`, `tb_fiscal_*`, etc.) are LEGACY — DO NOT WRITE TO THEM.** No active writer; `ingest.py` was deleted in the V6 cutover; EconDelta never wrote them. If you see code referencing those tables, it's stale and the call site needs deleting, not "fixing."

10. **Don't OCR Anthropic tokens from screenshots.** OCR confuses `0`/`O`/`o` and `l`/`I`/`1` on 140-character tokens. Always paste as TEXT.

11. **Multi-`&&` chained sudo over non-interactive SSH kills the session** (observed on Hetzner; assume the same for ExonVPS until proven otherwise). One sudo per ssh invocation works.

12. **Conductor workspaces** — if Adnan is working from `/Users/adnanrashid/conductor/workspaces/econdelta/<some-branch>`, expect: (a) `gh pr merge --squash --delete-branch` prints `fatal: 'main' is already used by worktree at ...` locally but the server-side merge succeeds (verify with `gh pr view <N> --json state,mergeCommit`); (b) Conductor's session hook denies `git reset --hard` — use `git checkout -b <new-branch> <ref>` to recreate the branch pointer at the desired ref instead.

13. **Mac laptop launchd backup MUST stay enabled.** `com.clauding-lab.econdelta.bb-forex` (06:05 BDT) and `com.clauding-lab.econdelta.dse-market` are belt-and-suspenders feeds that have covered ExonVPS gaps multiple times. Their `run-and-sync.sh` rsyncs the freshly scraped JSON onto ExonVPS, so the file mtime you see in `data/<indicator>/<date>.json` may be the Mac's sync time, not the ExonVPS scraper's write time.

14. **`/etc/econdelta.env`** (mode 640, owner `root:adnan-local`) holds `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_BINARY=/usr/bin/claude`, Supabase service-role key, and scraper config. **Never `cat` this file in shell output, never commit it, never paste its contents into a chat.**

15. **`docs/indicator-catalog.md` is auto-generated.** Don't hand-edit. Edit `scripts/build_catalog.py` (for derived-key entries) or `config/sources-v3.json` (for scraped indicators), then regenerate.

16. **A new parser must be wired into `parse_all.py`'s auto-import block.** Adding `parsers/<x>.py` (with its `@register("<x>")` decorator) plus a `sources-v3.json` entry is NOT enough — the decorator only runs on import, and production builds `REGISTRY` from the explicit `import parsers.<x>` list near the top of `parse_all.py`. Forget it and you get `parse_one raised … "no parser registered for '<x>'"`, the indicator silently produces 0 rows, and unit tests STILL pass (they import the parser module directly, which triggers the decorator). Guard: `tests/test_parser_registry_coverage.py` runs in a subprocess and fails if any `sources-v3.json` deterministic parser isn't registered via `parse_all`'s own imports. (2026-05-29, PR #35 — the policy-rate corridor was dark for a day.)

17. **Services that shell out to `claude` under `ProtectHome=read-only` need `~/.claude.json` in `ReadWritePaths`, not just `~/.claude/`.** The CLI writes its state file `~/.claude.json` (a sibling of the `.claude/` dir) on each run; under the read-only home that write hits `EROFS`, the parse preflight fails all 3 attempts, and `parse_all` aborts (exit 1) before parsing anything. `econdelta-parse` and `econdelta-aggregate` carry `*.service.d/10-claude-json-writable.conf` drop-ins (committed in `deploy/`) that add the carve-out. Diagnose sandboxed-service failures in `logs/<unit>-systemd.log` (the unit's `StandardError=append:` target), NOT journald — that's why the failure was invisible for 4 days. (2026-05-29 — every daily Supabase value was a stale carry-forward while parse was dead.)

18. **No Supabase read helper in Python — reads are hand-rolled GETs.** `utils/supabase_writer.py` is POST/PATCH only; `opus_review.load_history` reads LOCAL `data/archive/latest_*.json`, not Supabase. The weekly briefing job's `utils/supabase_reader.py` adds the read side (PostgREST `GET` via `requests`, mirroring the writer's `apikey`+`Bearer` auth headers, raising `SupabaseReadError`). Daily `metric_history` has only a `service_role_all` RLS policy (no anon-read), so the briefing job reads with the **service-role** key — NOT the anon key the PWA uses. (2026-05-30, weekly briefing pipeline.)

19. **`deploy/install.sh`'s timer-enable loop is a hardcoded list, not a glob.** The unit-copy step globs `econdelta-*.service|.timer` so new files are copied automatically — but the `systemctl enable --now` loop names each timer explicitly. A new `econdelta-*.timer` gets copied but NEVER enabled unless added to that loop. `econdelta-briefing` was added 2026-05-30; the next new timer must be too. (Also the first weekly `OnCalendar=Mon …` timer — all others are daily `*-*-*`.)

20. **Two parallel metric systems — don't mix namespaces.** DAILY: `metric_history` + `metric_definitions`, ids from `config/sources-v3.json` (e.g. `call_money_rate`, `tbill_182d_yield`, `policy_rate_repo`). MONTHLY: `metric_history_monthly` + `metric_definitions_monthly`, ids from `scripts/seed_macro_monthly.py` KEY_MAP, all suffixed `_monthly`. They double-count concepts (CPI, policy rate appear in both). The weekly briefing reads the DAILY system (what YieldScope surfaces). Note: policy rates + CPI are `monthly` cadence even within the daily pipeline, so weekly anomaly diffs on them are usually flat — they fire on month-change. (2026-05-30, weekly briefing pipeline.)

21. **`claude` effort levels differ by CLI version — the briefing job needs `BRIEFING_EFFORT=max` on the box.** The pinned ExonVPS CLI (`claude 2.1.104`, see landmine 1) accepts `--effort low|medium|high|max`; `xhigh` (the briefing's code default, valid on newer CLIs) is REJECTED with `option '--effort' argument 'xhigh' is invalid`. The orchestrator reads a `BRIEFING_EFFORT` env override — set `BRIEFING_EFFORT=max` in `/etc/econdelta.env` on the box (`install.sh`'s scaffold now seeds it for fresh installs; the existing live env file must be edited by hand). Separately, whether `opus[1m]` (the briefing's default model) resolves on 2.1.104 is UNVERIFIED — if it errors set `BRIEFING_MODEL=claude-opus-4-8` (the string the 4.8 bump uses); if THAT also fails the CLI predates Opus 4.8 entirely (don't blind-upgrade — landmine 1). (2026-05-30, briefing deploy.)

22. **`upsert_metric_history(url=...)` is the Supabase base-URL OVERRIDE, not a provenance field.** `_resolve_credentials` does `resolved_url = url or os.environ["SUPABASE_URL"]`, so passing a source-page URL there silently POSTs the write to the SOURCE host. The Tier-2 scrapers `imf_eff` / `imf_debt_gdp` / `world_bank_pink_sheet` each passed their source URL → writes went to `www.imf.org` / `thedocs.worldbank.org` (a 301→2xx page logged as "upserted N rows" but persisting NOTHING, or a 404 Adobe-Helix page), while `run_logs` (no override) landed — so logs looked clean but `metric_history` stayed empty. `metric_history` has NO `url` column; `source` carries provenance. NEVER pass `url=` from a scraper, and verify a write LANDED (re-query the table) rather than trusting a 2xx / "wrote N rows" log. (2026-06-01, PR #55.)

23. **IMF + World Bank hosts have IPv6 blackholed from the ExonVPS box — force IPv4 for those fetches ONLY.** `www.imf.org` and `thedocs.worldbank.org` time out on `curl -6` (~25s) but work on `-4` (~1.5s); a default dual-stack fetch hangs on the dead AAAA until systemd kills the one-shot service (`run_logs` shows `status=running`, never finished). Wrap those fetches in `utils/ipv4.force_ipv4_only()` (saves → sets urllib3 `HAS_IPV6=False` → restores in `finally`) — scope to the FETCH only; never leave the global flipped (Supabase is IPv4-only/no-AAAA so a bleed is harmless there, but it's still a latent foot-gun). New scrapers hitting other blackholed hosts should reuse this helper, not re-pin `HAS_IPV6` inline. (2026-06-01, PR #54.)

24. **BB auction RESULTS + CALENDAR come from `monetaryactivity/treasury` + `auc_calendar/1` — NOT the `/rrpt/` press release or the bare `auc_calendar`.** BB retired the per-business-day `/rrpt/` auction-result press release (now a PDF at `mediaroom/press_release/press/pr<id>_<date>.pdf` behind an F5 BIG-IP + image-CAPTCHA wall the renderer/solver CANNOT clear — 5 retrieval methods proven to fail; do not waste time re-attacking it). `scrapers/bb_auction.py` instead solver-fetches `monetaryactivity/treasury` (a 2-row grouped-header HTML table — `Bids received` colspan-3 / `Bids accepted` colspan-7, so map columns by header GROUP, landmine E) via `parse_treasury_results`, and the forward calendar from `auc_calendar/1` ("Yearly calendar" — a CSS div-grid of `div.row-header`+`div.row-data`/`div.column`, two grids bills-then-bonds in document order) via `parse_yearly_calendar`. The bare `auc_calendar` ("Yet to bid") no longer renders a `<table>`. `auction_date` (results) is the table's *Issue date* (settlement, ~1 business day after held). The scalar `slf_draw_cr`/`bb_repo_usage_cr` (OMO "Open Market Operations as on…" press release) were RETIRED in PR #62 — that release is also a walled PDF, no HTML source carries the SLF/Repo accepted amounts, and 7 retrieval methods (incl. an in-iframe solve with `claude-sonnet-4-6`) failed; deleting them removed the last `/rrpt/` consumer, so `fetchers/rrpt_discovery.py` + `parsers/html_auction_press_row.py` + the `fetch_all` `latest_rrpt_link` branch are GONE. Do not re-add a `/rrpt/` or OMO-PDF fetch path. (2026-06-02, PR #59 + #62.)

25. **PostgREST bulk-upsert (a POSTed JSON array) 400s with PGRST102 "All object keys must match" unless EVERY object carries the same keys.** Auction RESULTS rows are heterogeneous (bonds have `wam`, bills don't), so a mixed batch is rejected wholesale — even though each row is individually valid. `utils/supabase_writer._validate_auction_rows` unions the keys present across the batch and fills each missing column with `None` (real SQL NULL, never a fabricated 0). Any new row-table writer that batches heterogeneous rows must do the same; a homogeneous batch (e.g. the calendar) is unaffected. (2026-06-02, PR #60.)

26. **`source_as_of` must be recovered on the LLM-extract fallback AND propagated through `BRIEF_ALIASES`/`BRIEF_CONVERSIONS`.** The QFSAR (NPL/CAR) parses via `pdf_component`, which FAILS on the report's exec-summary prose → `hybrid.parse_one` falls to the LLM extract path. That path used to drop `source_as_of`, so a quarterly metric got stamped with TODAY's run date and a stale Q3-2025 NPL (35.73%) read as "fresh" on The Brief. Fix (PR #64): `PdfComponentParser.recover_source_as_of` runs in the LLM fallback; `_extract_quarter_end` now also matches the report's real idiom "(as of) end-<Month> YYYY" and "Month–Month YYYY" — NOT only "Quarter ending DD Month YYYY". CRUCIALLY (PR #65): the brief reads the brief-side ALIAS (`banking_npl_pct`), not the EconDelta id (`gross_npl_ratio`), so `_build_source_as_of_map` MUST propagate each recovered date to its `BRIEF_ALIASES`/`BRIEF_CONVERSIONS` target or the SPA never sees it. `_build_source_as_of_map` also WARNS when a `quarterly`/`fiscal_year` metric has no `source_as_of` — those warnings (`debt_gdp_ratio`, `gdp`, `fy_*`, debt stocks) flag the next parsers needing date recovery. `as_of` = the reporting period, NEVER the run date. (2026-06-04, PRs #64/#65.)

27. **Media-screen (`media_screen/` + `scrapers/media_screen.py`) — precision is load-bearing.** Daily timer (21:30 BDT, landmine 19) screens Daily Star + TBS banking sections for fresher/conflicting BB figures → inserts into the `media_review` queue (migration `0010`) + one Discord digest; approve/reject flips status (Copotron via forced-command SSH `deploy/media-decide-ssh.sh`, or `python -m media_screen.decide approve|reject <id>` — race-safe conditional `status=eq.pending` PATCH, repeat = no-op); `aggregate_latest._apply_media_overrides` runs AFTER the normal upsert, applies `approved` rows at the press period (`source='media-approved:<outlet>'`), and supersedes once BB's pipeline reaches that period or revises it (D6). EconDelta stays the SOLE `metric_history` writer (Phase-1 screen writes ONLY `media_review`). Every `MetricSpec` needs a `valid_range` unit guard (a % ratio must reject Tk-crore amounts like 588704) AND the extraction prompt must return ONLY the overall sector-wide figure — without both, ONE NPL article yields ~13 junk candidates (amounts-as-ratios, per-segment ratios mislabelled as the sector). Section-path article patterns: Daily Star banking articles live at `/business/news/` + `/business/economy/news/`, NOT `/business/banking/`. (2026-06-04, PRs #66/#67/#68.)

28. **Media-screen daily report + the `approve N` = `media_review.id` contract + catalog alias uniqueness.** (Extends landmine 27.) `run_screen` now ALWAYS posts a report — even a 0-candidate run posts a "no change" heartbeat listing each tracked figure it saw and WHY it was skipped. `classify` is a TOTAL function `Candidate | Skip` (5 reasons: `out-of-range`/`no-period`/`matches-current-data`/`older-period`/`already-in-review`); a `Skip` must NEVER reach insert — `run_screen` routes on `isinstance(c, Candidate)` (the old `if c is not None` would insert a truthy Skip). The report posts to **`MEDIA_SCREEN_WEBHOOK_URL`** (#thebrief), which is REQUIRED: if unset, `run_screen` logs + SKIPS the post — it must NOT pass `webhook_url=None`, which would silently fall back to the ops `DISCORD_WEBHOOK_URL` and misroute the report; operational errors DO stay on `DISCORD_WEBHOOK_URL`. CRITICAL: the digest numbers each candidate by its REAL `media_review.id`, NOT the list position — `insert_media_review_rows` returns the inserted ids (`Prefer: return=representation`) and `run_screen` inserts-then-formats (`zip(ids, candidates)`). The pre-fix digest numbered by `enumerate` index and only "worked" in the first live test because ids 1,2 happened to equal positions 1,2; `approve N` would silently hit the wrong/old row on every later run. The catalog matcher is last-writer-wins (`{n.lower(): spec}`), so EVERY press alias must be UNIQUE across all specs — `tests/test_media_catalog.py::test_no_alias_collisions` guards it. Catalog is 13 metrics as of PR #69 (added repo/call-money rates, 91d T-bill yield, food inflation, FY remittance/export, NBR revenue, USD/BDT); `usd_bdt_exchange_rate` tracks the BB crawling-peg MID rate, so press segment rates (interbank/selling/kerb) usually skip. Copotron's Hetzner `~/CLAUDE.md` block + the report channel are now #thebrief. (2026-06-04, merge `6fa68b5` + PR #69 `fd71264`.)

29. **PDF `source_as_of` recovery: gate report-detection on PDF CONTENT (not URL host), and pick the LATEST idiom match (not the first).** When adding date recovery to a parser shared across sources (`pdf_table_row` serves 25 indicators / 4–5 report families), two traps bite. (a) **Host gating is wrong** — `fetch_all.py` reassigns `url` to the discovered PDF link before `fetch_pdf`, so `FetchResult.source_url` is the RESOLVED link. For MoF that's a third-party object store (`objectstorage.…oraclecloud15.com`), NOT `mof.gov.bd`; gating on the config host returns `None` for every MoF metric. Identify the report by a stable CONTENT marker — its own title in the extracted text (`"major economic indicators"` / `"quarterly debt bulletin"`, the FULL title so a sibling MoF fiscal report that merely *mentions* "debt bulletin" isn't mis-gated). (b) **First-match is a stale-date trap** — these gov reports print comparison/prior dates (`as of 30 June 2024`, `up to Jun FY25`); `.search()` first-match can lock onto the STALE one, defeating supersession (the NPL-class bug this recovery exists to prevent). Use `finditer` + take the LATEST date. Idioms confirmed against live PDFs: BB MEI `Monthly Update (April 2026)` / `Volume 04/2026 April 2026` → month-end; MoF `As of 31 December 2025` (preferred, exact day) else `up to Dec FY26` (BD fiscal: Jul–Dec = prior calendar year) → quarter-end. A malformed preferred-idiom day (`31 February`) returns `None`, never falls through to the unrelated fallback's month. Unrecognised report / no date → `None` (the slow-cadence guard catches it; never fabricate). Recovery is isolated so it can never break value extraction; mirrors `pdf_component.recover_source_as_of` for the LLM path. `recover_source_as_of` is forward-looking (dates the NEXT parse; existing rows need a re-run to backfill). (2026-06-04, PR 1 / merge `6a42a15`. PR 2 = `pdf_component` WSEI fiscal-year metrics, still open.)

30. **The `Tests` CI (`.github/workflows/test.yml`) MUST install the Playwright headless shell before `pytest` — the suite is NOT fully browserless.** Two `tests/test_html_fetcher.py` cases launch a REAL headless Chromium against a `file://` page (not mocked), so they pass locally only because a browser is already installed; a clean runner fails with `BrowserType.launch: Executable doesn't exist …chrome-headless-shell`. The job runs `python -m playwright install --with-deps --only-shell chromium` (the `--only-shell` is enough — the fetcher runs headless). ruff + pytest fire on push-to-main and every PR; the suite is otherwise secret-free (env-touching tests use fakes), so no Actions secrets are needed. Don't delete the playwright step or assume "the suite is hermetic." (2026-06-05, PR #70 / merge `c27143a`.)

31. **Library/framework API calls → Context7 first.** Before writing or editing code that calls a third-party library or framework API, query **Context7** for current, version-pinned docs — do NOT rely on training-cutoff memory.
    - **Flow:** `resolve-library-id` (name → `/org/project` ID) → `query-docs` (PIN the version this repo ships, e.g. `/pydantic/pydantic/v2`).
    - **Applies to:** pydantic v2 (`>=2.8`), playwright (`>=1.49`) + playwright-stealth (`>=2.0`), pdfplumber (`>=0.11`), beautifulsoup4 (`>=4.12`), requests (`>=2.32`), yfinance (`>=0.2.40`), pytesseract / pdf2image / Pillow.
    - **Skip for:** business/domain logic, general programming concepts, or libraries Context7 does not index.
    - **Query specifically:** library + version + exact task (e.g. `pydantic v2 model_validator mode=before for sources-v3 indicator specs`), never one-word topics like "auth".

32. **The MFR fiscal parser (`scripts/mfr_parser.py` + `scripts/backfill_fiscal.py`) only reliably handles FY26-and-forward; FY25/FY24 and older months are a hand-verified STATIC backfill, not a parse.** MoF reflows the Table-6/Table-4 columns between issues (an inserted "Revised Budget FYxx" column, a dropped prior-FY full-year column, trailing memo columns), so the value-anchor `+1/+2` offset mis-reads older reports (Apr-2025 read the FY25 revised budget 99,000 as April's single-month). Per-FY budget anchors live in `FY_BORROW_BUDGET`/`FY_NBR_BUDGET` (FY26 104000/499001 reproduce the live DB; FY25 137500/480000; FY24 132395/430000); `parse_one_mfr` raises (→ skip) for an unknown FY rather than mis-anchoring, and the FYTD self-check is a HARD GATE (`self_check_failures` drops non-reconciling months). Historical months are `STATIC_NBR_MONTHLY`/`STATIC_BORROW_MONTHLY` + `build_static_monthly_rows()`, run via `--static-only` (idempotent upsert). NBR is clean (self-check reconciles, `source='mof_mfr_static'`); **govt bank borrowing is RESTATED between issues** so it does NOT reconcile — its rows are provisional (`source='mof_mfr_static_provisional'`), and Aug-2024 is intentionally omitted. Don't try to "fix" the parser to cover old reports, and don't trust the FYTD self-check to validate borrowing. (2026-06-07, PR #72 / merge `957e68f`.)

## Communication & timezone

- **All times in BDT (UTC+6).** When generating timestamps, dates, or schedules, convert to BDT and label it. UTC appears in some systemd unit files and `scraped_at` ISO strings — convert before showing to Adnan.
- **Plain-English explanations** of technical terms in conversation, even obvious ones. Adnan reads but doesn't write code.
- **No emojis** in code or commits unless explicitly requested.
- **Short, scannable updates** — Adnan reads on mobile often.

## Out-of-scope behaviors

Do not, without explicit user sign-off:

- Modify `/etc/econdelta.env` on ExonVPS (or print its contents).
- Add dependencies in `requirements.txt`, `pyproject.toml`, or `pwa/package.json`.
- Edit `deploy/*.service` or `deploy/*.timer` (changes need VPS sudo + daemon-reload + careful reschedule timing — see landmine 5).
- Edit `.github/workflows/pwa-deploy.yml`.
- Edit `db/schema.sql` or anything under `supabase/migrations/` (changes hit the live, shared Supabase project). Migrations are applied with `supabase db query --linked -f supabase/migrations/<file>.sql` from a linked Mac checkout — **NOT `supabase db push`**, which fails because the DB is shared with The Brief (whose migrations aren't in this repo). Keep every migration idempotent. Never re-introduce `db/migrations/`.
- Bulk-remove indicators from `config/sources-v3.json` (single retirements with rationale are fine).
- Disable `ECONDELTA_SKIP_OPUS_REVIEW` gating in `aggregate_latest.py`.
- Run `git push --force` against any branch.
- Skip hooks (`--no-verify`, `--no-gpg-sign`, etc.).
- Push tags (no tag-driven releases here; tag pushes have no consumer but signal intent that doesn't exist).

For everything else, see `VISION.md` for what auto-merges vs needs sign-off.

## Cross-cutting rules

Adnan's global rules live in `~/.claude/CLAUDE.md` (loaded automatically by Claude Code). When that file conflicts with this one, this file wins because it's project-specific.
