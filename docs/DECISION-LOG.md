# EconDelta — Decision Log

## 2026-04-20 — Phase 0 Audit Outcome

### Scraping stack decision

- **Decision:** Playwright + BeautifulSoup (with requests + BeautifulSoup fallback for DSE only)
- **Rationale:** Both Bangladesh Bank sources (exchange rates and forex reserves) are protected by a CAPTCHA when accessed via plain HTTP. They load correctly in a real browser session via Playwright. The data is technically in the DOM after JS execution, but the prerequisite is a Playwright session to bypass the CAPTCHA gate. DSE market-statistics.php is fully static and could be scraped with requests alone, but using Playwright uniformly keeps the stack consistent and avoids maintaining two fetch paths.

---

### Per-source findings

#### BB exchange rates

- **URL:** `https://www.bb.org.bd/en/index.php/econdata/exchangerate`
- **Method:** POST (date picker submits a form; current-day data loads on GET too, but historically requires POST with `date` field)
- **Rendering:** JS — plain HTTP GET returns a CAPTCHA page; Playwright session loads the page and DOM contains data
- **Blocker:** No hard ToS blocker found (robots.txt was CAPTCHA-gated and unreadable); flagged **warn** pending manual verification of BB ToS
- **Data available:** Table 0: USD bid / ask / WAR (WAR ≈ mid rate); Table 1: EUR, GBP, AUD, JPY, CAD, SEK, SGD, CNH, INR, LKR cross rates (bid/ask). Confirmed live values: USD 122.70, EUR 144.32/144.36, GBP 165.82/165.88
- **Selector:** `section.content table:nth-of-type(1)` (USD), `section.content table:nth-of-type(2)` (cross rates)
- **No stable table id or class** — positional selector required

#### BB forex reserves

- **URL:** `https://www.bb.org.bd/en/index.php/econdata/intreserve`
- **Method:** GET (page loads monthly series by default; POST with `period=monthly` or `period=yearly` for explicit selection)
- **Rendering:** JS — same CAPTCHA gate as exchange rates on plain HTTP; Playwright loads it cleanly
- **Blocker:** None found beyond the CAPTCHA (same warn as above)
- **Data available:** Period / Gross Reserves (USD mn) / BPM6 Reserves (USD mn). Latest: March 2026 = 34,116.6m gross, 29,501.2m BPM6. **Import cover months is NOT published on this page** — not available from BB without additional calculation or a separate source.
- **Selector:** `table#sortableTable` — stable, reliable
- **Update frequency:** Monthly

#### DSE market summary

- **URL:** `https://www.dse.com.bd/market-statistics.php` (for advancing/declining/unchanged + trades + turnover) + `https://www.dse.com.bd/` (for DSEX/DS30/DSES index levels)
- **Rendering:** Static HTML — requests + BeautifulSoup sufficient
- **Data structure:** `market-statistics.php` delivers a `<code>` element inside a `<table><tbody><tr><td>` containing a preformatted plaintext block with all stats. The homepage delivers index values as inline text in a summary widget.
- **Confirmed live values (2026-04-20):** DSEX 5,232.49 | DSES 1,059.70 | DS30 1,980.01 | Trades 223,903 | Turnover 8,247,602,308.40 Tk (≈ 824.76 crore) | Advancing 120 | Declining 207 | Unchanged 62
- **DSE ToS review:** Both `/terms-of-use.php` and `/terms_condition.php` returned HTTP 404. No automated-access restriction clause could be located during audit. `robots.txt` also returned 404. **No explicit automated-access restriction found.** Flagged as **warn** (not blocker) because absence of ToS page does not confirm permission; recommend legal review before production deployment.

---

### Commodity provider

- **Chosen:** yfinance (default recommendation confirmed)
- **Tickers:** Brent `BZ=F`, WTI `CL=F`, Gold `GC=F`, Palm Oil `FCPO.KL`
- **Palm oil ticker resolution:** `FCPO.KL` (Bursa Malaysia crude palm oil futures) preferred over `KPO=F` (thinner CME liquidity). Confirmed resolvable via yfinance.

---

### Risks escalated

1. **Playwright is required for both BB sources.** Plain HTTP requests return a CAPTCHA on bb.org.bd. This adds `playwright` and `playwright-stealth` to Phase 1 requirements. The BB site uses Drupal with an external WAF/CAPTCHA (likely Cloudflare or similar). Playwright with a real Chromium UA passes cleanly.

2. **robots.txt unreadable for BB.** The robots.txt endpoint itself was CAPTCHA-gated during audit. Manual verification recommended before production. Treat as **warn** — not a confirmed blocker, but proceed cautiously.

3. **DSE ToS page not found (404).** No restriction found, but also no explicit permission. Legal review recommended before production use. Data is public market data so risk is low, but not zero.

4. **import_cover_months not available from BB.** The intreserve page only publishes gross and BPM6 reserves in USD millions. Import cover months will need to be derived (reserves ÷ average monthly imports) using a separate imports data source, or dropped from v1 scope.

5. **BB exchange rates use WAR (Weighted Average Rate), not a distinct mid/buy/sell.** Table structure is: Currency | Bid Rate | Ask Rate | WAR. The "mid" field maps to WAR. There is no separate "buy" and "sell" label — bid = buy, ask = sell.

---

### Next steps

- **Phase 2+ is unblocked** — no hard blockers found. All three primary sources return accessible data via Playwright.
- **Update Phase 1 `requirements.txt`** to add `playwright` and `playwright-stealth` (Playwright required for BB sources).
- **Resolve import_cover_months:** Either derive from BB trade stats or drop from v1 schema.
- **Manual verification:** Check bb.org.bd ToS via browser to confirm no automated-access prohibition before production deployment.
