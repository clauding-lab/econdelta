# Vision

EconDelta is the canonical data layer for Bangladesh economic indicators — ~60 series, daily, on a deterministic schedule. It should keep expanding indicator coverage and increasing parse reliability while preserving the things downstream consumers depend on: stable indicator IDs, stable Supabase `metric_history` schema, daily-by-default cadence, anomaly gates that refuse to publish bad data, and the principle that **the safe path is the default** (a missed scrape lets yesterday's value carry forward; a junk scrape never overwrites a good one).

The rules below scope what AI agents and contributors can ship without explicit sign-off.

## Merge by Default

- Bug fixes with clear cause and bounded blast radius.
- Documentation, README, code-comment fixes; regenerated `docs/indicator-catalog.md`.
- Small UI/UX tweaks in `pwa/` that don't change layout, copy, or behavior materially.
- New tests, including coverage for existing code.
- Logging additions and small observability improvements (better `notifier.py` payloads, more verbose `journalctl` lines).
- Extensions to existing patterns: new scraper in `scrapers/` following the existing shape, new parser in `parsers/registry.py`, new entry in `BRIEF_ALIASES` or `BRIEF_CONVERSIONS`.
- Internal refactors confined to a single module that don't change the external surface and keep tests green.
- Dependency patch-version bumps — *except* `playwright`, `playwright-stealth`, `pdfplumber`, `pydantic` (those need scrutiny on any version change).
- Single indicator retirements from `config/sources-v3.json` when paired with a rationale and a fallback path for any derived value (e.g. NBR FYTD news corroborator retirement, 2026-05-25).

## Needs Sign-Off

- **New features** — any change to user-visible PWA behavior, any new Discord alert category, any new aggregate-level cross-check.
- **Dependency additions** in `requirements.txt`, `pyproject.toml`, or `pwa/package.json`.
- **Dependency minor or major bumps**, and any bump of: `playwright`, `playwright-stealth`, `pdfplumber`, `pydantic`, `claude` CLI.
- **Toolchain / runtime version changes** — Python version, Node version, OS-level binaries (`tesseract`, `poppler-utils`).
- **Broad refactors** that span >1 module or touch a public boundary (Supabase schema, indicator IDs, `latest.json` shape, brief-side `tb_*` reading patterns).
- **Architectural changes** — new top-level dirs, new orchestrator scripts beside `fetch_all.py` / `parse_all.py` / `aggregate_latest.py`, new long-running processes.
- **Release pipeline edits** — `.github/workflows/pwa-deploy.yml`, anything in `deploy/` (systemd units, install scripts, logrotate).
- **`/etc/econdelta.env` content** — adding new env vars or rotating existing ones requires sign-off because the file lives on the VPS with root ownership.
- **`db/schema.sql` or `db/migrations/`** — these hit the live Supabase project.
- **Privacy / network surface changes** — telemetry, new outbound destinations, log content that could include sensitive data.
- **Load-bearing semantics** — indicator IDs already in production, Supabase column names, the `nbr_fytd_collected_cr` canonical source, the daily cadence assumption.
- **Bulk indicator retirements** (>1 indicator) from `config/sources-v3.json` — even with rationale.
- **Disabling Opus aggregate review** (`ECONDELTA_SKIP_OPUS_REVIEW`) anywhere outside a one-off debug session.

## When in doubt

If a change could conceivably surprise the user, ask first. Cost of one extra question << cost of one bad surprise.
