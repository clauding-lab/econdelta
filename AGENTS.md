# AGENTS.md — EconDelta

Operational rules for AI coding agents (Claude Code, Cursor, Codex CLI, etc.) working in this repo. Read this in full before making any code change.

## What this project is

EconDelta is the **data layer** for Bangladesh economic indicators. ~60 series scraped daily from Bangladesh Bank, BBS, NBR, DSE, DAM, and news outlets; parsed (deterministic regex / table extractors + Claude LLM fallback); written to a canonical `data/latest.json` snapshot AND a row-per-indicator-per-day in Supabase `metric_history`. Python 3.11+, deployed via systemd timers on ExonVPS (`adnan-local@103.187.23.22`, BDIX-hosted Dhaka). A Mac-laptop launchd path is a belt-and-suspenders backup for two flaky scrapers. Downstream apps (The Brief, Mission Control) read from Supabase — they do NOT depend on EconDelta's Python code. The dashboard PWA at `econdelta.clauding-lab.com` (Next.js, GitHub Pages) is the human surface.

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
db/                   schema.sql + migrations/ (Supabase)
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
| VPS deploy (backend) | `ssh adnan-local@103.187.23.22 'cd ~/econdelta && git pull origin main'` |
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
- Edit `db/schema.sql` or anything under `db/migrations/` (changes hit the live Supabase project).
- Bulk-remove indicators from `config/sources-v3.json` (single retirements with rationale are fine).
- Disable `ECONDELTA_SKIP_OPUS_REVIEW` gating in `aggregate_latest.py`.
- Run `git push --force` against any branch.
- Skip hooks (`--no-verify`, `--no-gpg-sign`, etc.).
- Push tags (no tag-driven releases here; tag pushes have no consumer but signal intent that doesn't exist).

For everything else, see `VISION.md` for what auto-merges vs needs sign-off.

## Cross-cutting rules

Adnan's global rules live in `~/.claude/CLAUDE.md` (loaded automatically by Claude Code). When that file conflicts with this one, this file wins because it's project-specific.
