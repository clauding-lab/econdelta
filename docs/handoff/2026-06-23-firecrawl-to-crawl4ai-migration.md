# Handoff: Migrate EconDelta off Firecrawl → crawl4ai + deterministic PDF parse

**Date:** 2026-06-23
**Source session:** AI Productivity Setup (Firecrawl-replacement tool eval — crawl4ai chosen + installed locally)
**Target session:** EconDelta repo — further exploration / migration
**Scope:** Replace EconDelta's two Firecrawl dependencies (PDF fetch+parse; link discovery) with self-hosted **crawl4ai** for acquisition/discovery and a **deterministic PDF parser** (Docling or the existing pdfplumber path). Live daily systemd timers stay untouched until the backfill paths are validated. **SearXNG is NOT in scope** — EconDelta uses Firecrawl's *scrape*, not its *search*.

> **How to use this doc:** Open it at the start of an EconDelta session. Read `AGENTS.md` + `VISION.md` + `AGENT_LEARNINGS.md` first, then this. It is self-contained — you should not need the originating chat.

---

## TL;DR

EconDelta calls Firecrawl in two backfill scripts: to **fetch+parse Bangladesh Bank PDFs** and to **discover PDF links** off the MFR archive. Both cost credits (the fiscal series is capped at ~4 months *because of* the credit budget), and the PDF parse is **non-deterministic** (same PDF → different table placement across re-fetches) — a live data-quality landmine for a warehouse that feeds The Brief.

The migration isn't just cost-cutting. Three wins at once: **(1)** drop the credit cap, **(2)** replace the non-deterministic Firecrawl PDF parse with a **deterministic local parser**, and **(3)** lean on infra EconDelta *already has* (`fetchers/pdf_fetcher_stealth.py`). crawl4ai is already installed and verified locally — see §5.

---

## 1. Why now (the trigger + the real prize)

- The Firecrawl-replacement thread concluded crawl4ai is the closest free, open-source match for the scrape/crawl job. It's installed and smoke-tested on the Mac.
- The **eureka**: EconDelta's own code flags Firecrawl's PDF parse as non-deterministic (`backfill_call_money_monthly.py:308` — "across Firecrawl re-fetches of the SAME PDF the layout is non-deterministic"). A deterministic local parser is a *correctness upgrade*, not just a cost swap. For a credit-data warehouse, that matters more than the credits.

---

## 2. Current Firecrawl footprint (verified 2026-06-23)

| File | What it does | Pattern / endpoint | Key lines |
|---|---|---|---|
| `scripts/backfill_call_money_monthly.py` | Fetch a BB PDF → parsed markdown, then parse tables | `POST https://api.firecrawl.dev/v2/scrape` with `parsers=["pdf"]`, `proxy="stealth"`; needs `FIRECRAWL_API_KEY` | `_FIRECRAWL_ENDPOINT` ~438; `_fetch` ~448–468; non-determinism note ~308 |
| `scripts/backfill_fiscal.py` | Discover MFR PDF URLs off the archive page | `discover_mfr_pdf_links(scrape_fn=…)`; `_firecrawl_scrape` placeholder using `firecrawl stealth waitFor=9000` | `discover_mfr_pdf_links` ~295; `_firecrawl_scrape` ~393–410 |

Both are **one-shot backfill scripts**, not the live daily timers — so they're the safe place to migrate first.

`discover_mfr_pdf_links` already takes `scrape_fn` as an **injected dependency** (for tests) → swapping the fetcher is a clean, testable change.

---

## 3. What EconDelta ALREADY has (don't rebuild)

- **`fetchers/pdf_fetcher_stealth.py`** — Playwright-stealth (`playwright_stealth.Stealth`) fetcher for **Akamai/Radware-protected** BB PDFs. It primes an HTML page on the same domain first, then pulls the PDF, and parses with **`pdfplumber`**.
- So EconDelta has homegrown stealth acquisition **and** a local PDF parser already. The Firecrawl dependency in the backfills is partly redundant with this.
- **Implication:** the migration may not even need crawl4ai for the *PDF* path — `pdf_fetcher_stealth.py` + a parser could suffice. crawl4ai's clearest win is the **link-discovery** path (HTML archive → URLs).

---

## 4. Replacement mapping

| Firecrawl job | Replacement | Notes |
|---|---|---|
| **Link discovery** (MFR archive → PDF URLs) | **crawl4ai** (crawl the archive page, extract links) | Cleanest swap; `scrape_fn` injection makes it low-risk + unit-testable |
| **PDF acquisition** (get the PDF past defenses) | **Existing `pdf_fetcher_stealth.py`** first; crawl4ai as fallback | From the **Dhaka VPS the BD IP already bypasses the geo-block** Firecrawl stealth was working around — plain/stealth fetch likely enough |
| **PDF → structured tables** | **Docling** (deterministic, best-in-class tables) *or* existing **pdfplumber** | Fixes the non-determinism landmine. Docling = better tables; pdfplumber = already in-repo, fewer deps |
| **Web search** | — | N/A. EconDelta doesn't use Firecrawl search. (SearXNG would only matter for feeddeck.) |

---

## 5. crawl4ai status (already done)

- Installed + verified **locally on the Mac** (not yet on the VPS): isolated **uv venv, Python 3.12**, at `~/Projects/crawl4ai-lab/`, **crawl4ai 0.9.0**.
- Activate: `source ~/Projects/crawl4ai-lab/.venv/bin/activate`. CLI `crwl <url> -o markdown`; or `AsyncWebCrawler` in Python.
- Smoke-tested: BB Wikipedia → HTTP 200, 118K chars clean markdown, 825 links, 1.36s.
- Default markdown carries nav cruft → use crawl4ai's **"fit markdown"** (content-pruning) for clean output.
- (See auto-memory `project_crawl4ai_install.md`.)

---

## 6. Suggested migration plan (phased, low-risk)

1. **Local prototype (Mac, no VPS):** rebuild `discover_mfr_pdf_links`'s `scrape_fn` on crawl4ai against the MFR archive; confirm it returns the same PDF URL set Firecrawl did.
2. **PDF path:** test `pdf_fetcher_stealth.py` (+ Docling or pdfplumber) on a known BB call-money PDF; compare extracted numbers to a Firecrawl-era baseline.
3. **Validate before cutover:** golden-file compare new vs old numbers on a stable reference month. *(Caveat: Firecrawl's non-determinism means the "old" baseline is itself fuzzy — pick a hand-verified reference, not a Firecrawl re-fetch.)*
4. **VPS rollout:** install crawl4ai on the **Dhaka ExonVPS** (`103.187.23.22`, `adnan-local`). Playwright is likely already present (pdf_fetcher_stealth uses it) — verify. **Per-action SSH authorization required.**
5. **Cut over backfills first**, leave the 4 live timers on Firecrawl until backfills are proven, then promote. Keep `FIRECRAWL_API_KEY` path as a fallback initially.
6. Log the decision in `AGENT_LEARNINGS.md` + `DECISION-LOG.md`.

---

## 7. Risks & non-negotiables

- **Data fidelity over convenience.** EconDelta feeds The Brief and downstream analytics. A parser swap can shift historical numbers — validate before cutover (§6.3). Under-counting/silent drift is the dangerous direction.
- **Don't break the live pipeline.** 4 systemd timers run daily. Migrate the *one-shot backfills* first; never touch the live fetchers until the new path is proven.
- **VPS = production.** Any SSH to `103.187.23.22` is a per-action, explicitly-authorized step. BD-located on purpose (see auto-memory `feedback_bd_vps_for_bd_scraping`).
- **Python pin.** crawl4ai venv is on 3.12 deliberately (system Python is 3.14, bleeding-edge); on 3.12+ pin `setuptools<80` if `pkg_resources` breaks (auto-memory `feedback_python312_setuptools_pkg_resources`).

---

## 8. Open scoping questions (settle first)

1. **Scope:** backfills only, or the live daily fetchers too? (Recommend backfills first.)
2. **Parser:** Docling (better tables, new dep) vs existing pdfplumber (in-repo, simpler)?
3. **Does the PDF path even need crawl4ai,** or is `pdf_fetcher_stealth.py` + a parser enough — with crawl4ai reserved for link discovery?
4. **Validation reference:** what's the hand-verified golden month to compare against (given Firecrawl non-determinism)?
5. **Firecrawl fallback:** keep it wired as a fallback, or full removal once proven?

---

## 9. How to resume

1. Open a session in `~/Projects/clauding-lab/econdelta` (branch `main`).
2. Read `AGENTS.md` + `VISION.md` + `AGENT_LEARNINGS.md` + this handoff.
3. Answer §8, then execute §6 from step 1 (local prototype) — no VPS, no live-timer changes until validated.
4. crawl4ai is ready locally (§5). Start with the link-discovery swap; it's the cleanest, testable first win.
