# Cerebrum

> OpenWolf's learning memory. Updated automatically as the AI learns from interactions.
> Do not edit manually unless correcting an error.
> Last updated: 2026-04-20

## User Preferences

<!-- How the user likes things done. Code style, tools, patterns, communication. -->

## Key Learnings

- **Project:** econdelta
- **Description:** A deterministic Python pipeline that scrapes Bangladesh Bank, DSE, and commodity price data on a schedule, writes versioned JSON snapshots, and exposes a canonical `data/latest.json` consumed by The B
- **DSE homepage label parsing:** `m_col-1` label text strips to "DSEXIndex"/"DSESIndex"/"DS30 Index" (no spaces) because the `<font>` tag merges characters without separator. Use `re.sub(r"\s+", "", label.lower())` then check for "dsex"/"dses"/"30" substrings in that order.
- **DSE market-statistics.php code selector:** `table > tbody > tr > td > code` fails (no explicit tbody in source). Use `table code` or `code` as selector.
- **FetchError is a nested class on HttpClient:** `from utils.http_client import FetchError` fails with ImportError. Must use `HttpClient.FetchError` or assign `FetchError = HttpClient.FetchError` at module level.
- **DSE turnover unit:** market-statistics.php VALUE(Tk) is in raw Taka. Divide by 10,000,000 (10M) to get crore.

## Do-Not-Repeat

<!-- Mistakes made and corrected. Each entry prevents the same mistake recurring. -->
<!-- Format: [YYYY-MM-DD] Description of what went wrong and what to do instead. -->

[2026-04-20] DSE homepage: `table > tbody > tr > td > code` CSS selector fails because DSE omits explicit `<tbody>` tags. Use `table code` or `code` instead. Confirmed on live fixture 2026-04-20.

[2026-04-20] DSE label slugging: `m_col-1` in midrow divs merges "DSE" + "X"/"S" (from `<font>` tag) into "DSEXIndex"/"DSESIndex" with no whitespace when using `.get_text(strip=True)`. Must slugify by stripping all spaces before matching, not by checking for substrings like " x ".

[2026-04-20] FCPO.KL (Bursa Malaysia palm oil futures) returns HTTP 404 from Yahoo Finance — symbol is delisted/unavailable. The scraper handles this gracefully as a partial-fetch (exit 0 + warning). Do not assume FCPO.KL is available; the fallback FetchError path is the real path for this ticker until a live alternative is found.

[2026-04-20] BB reserves page (intreserve) never fires networkidle — always times out at 30s. Must use domcontentloaded + 5s wait_for_timeout fallback. Exchange rates page fires networkidle fine. The fetch_rendered_html function should try networkidle first then fall back automatically.

## Decision Log

<!-- Significant technical decisions with rationale. Why X was chosen over Y. -->

[2026-04-20] fetch_commodity() tries fast_info dict-style access first (cheap, no download), then falls back to history(period="5d") when fast_info is missing keys or raises. This dual-path strategy handles yfinance version differences and missing symbols gracefully.

[2026-04-20] EUR/GBP cross rates on BB exchangerate page: use mid = (bid + ask) / 2 — there is no WAR column for cross rates, only USD has WAR. This is documented in the scraper's docstring.

[2026-04-20] BB reserves table structure: row 0 = "(In million US $)", row 1 = column headers, subsequent rows alternate fiscal year headers ("2025-2026", single cell) and month data rows. The fiscal year group determines the calendar year for reserves_date. BD FY: Jul-Jun — Jan-Jun months belong to the end year.
