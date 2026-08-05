"""BB forex + reserves scraper — Playwright-driven, anomaly-gated, schema-validated."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from scrapers.bb_forex_captcha import ParseError, solve_captcha_loop
from utils.anomaly import check_threshold, load_thresholds
from utils.notifier import notify
from utils.parser import parse_number
from utils.schema import ForexRates, ForexReserves, ForexSnapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "bb_forex"
CONFIG_PATH = REPO_ROOT / "config" / "sources.json"
THRESHOLDS_PATH = REPO_ROOT / "config" / "thresholds.json"

# Fiscal-year header row on BB's reserves table, e.g. "2025-2026".
_FISCAL_YEAR_RE = re.compile(r"^\s*(\d{4})\s*-\s*(\d{4})\s*$")

# Fallback bounds for the month-advance sanity check (see main()) when
# config/thresholds.json is missing the gross_reserves_usd_bn_advance_drop /
# _jump keys -- normally those keys drive the check; these are a belt-and-
# suspenders default only.
_RESERVES_ADVANCE_DROP_DEFAULT = 0.10
_RESERVES_ADVANCE_JUMP_DEFAULT = 0.25

# Reserves table header-row label, accepted case-insensitively -- BB has used
# "Period" historically; "Month" / "End Period" are tolerated in case the
# label text drifts (2026-08-05 review finding M4).
_RESERVES_PERIOD_HEADER_LABELS = frozenset({"period", "month", "end period"})

# Tolerant BPM6 header match -- catches "BPM6", "BPM 6", "BPM-6" case-
# insensitively (2026-08-05 review finding H1: the previous exact "bpm6"
# substring match silently failed on "BPM 6"/"BPM-6" spellings, leaving
# bpm6_col=None and quietly disarming the cross-column invariant below).
_BPM6_HEADER_RE = re.compile(r"bpm[ -]?6", re.IGNORECASE)

# Cross-column sanity band for bpm6/gross (2026-08-05 review finding H2).
# BB's BPM6 methodology excludes items gross includes, so bpm6 is always
# BELOW gross -- but a same-direction magnitude/unit corruption (e.g. a
# decimal-place or million/billion mixup on just one of the two columns)
# would still pass a bare "bpm6 < gross" check while landing far outside any
# plausible reserves relationship. Verified against the full committed
# 27-month fixture history (scripts/_seed_data/bb_reserves_gross_bpm6_history.json,
# 2024-04..2026-06): the real observed ratio ranges 0.7643 (2024-11) to
# 0.8763 (2026-06) -- BPM6's share of gross has trended up over that window,
# it is NOT a tight constant ~0.876 (that figure reflects only the memo's
# narrow Nov-2025..Jun-2026 tail, not the fuller history). [0.70, 0.95] keeps
# ~6pp of margin below the lowest real observation while still catching
# order-of-magnitude corruption by a wide margin (a 122x-scaled column, the
# review's reproduced case, lands the ratio near 0.007 or above 100 -- nowhere
# close to this band either direction).
_BPM6_GROSS_RATIO_MIN = 0.70
_BPM6_GROSS_RATIO_MAX = 0.95

logger = logging.getLogger("bb_forex")

# ParseError re-exported from scrapers.bb_forex_captcha so existing callers
# importing it from scrapers.bb_forex keep working.
__all__ = [
    "DATA_DIR",
    "ParseError",
    "fetch_rendered_html",
    "load_previous_snapshot",
    "main",
    "parse_exchange_rates",
    "parse_reserves",
    "write_snapshot",
]


def _fetch_once(
    url: str,
    timeout_ms: int,
    wait_for_selector: str | None,
) -> str:
    """Single browser-launch attempt. Caller wraps with retry."""
    stealth = Stealth()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="Asia/Dhaka",
        )
        page = context.new_page()
        stealth.apply_stealth_sync(page)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(10000)

            # CAPTCHA loop — solves BB's image challenge if served. Short-
            # circuits immediately when no captcha is present. See
            # scrapers.bb_forex_captcha for marker logic and solver details.
            html = page.content()
            html = solve_captcha_loop(page, html, timeout_ms)

            if wait_for_selector is not None:
                try:
                    page.wait_for_selector(wait_for_selector, timeout=15000)
                except Exception:
                    logger.warning(
                        "selector %s not found on first load — reloading (challenge cookies should now be set)",
                        wait_for_selector,
                    )
                    page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_timeout(5000)
                    page.wait_for_selector(wait_for_selector, timeout=20000)

            html = page.content()
        finally:
            browser.close()
    return html


def fetch_rendered_html(
    url: str,
    timeout_ms: int = 60_000,
    wait_for_selector: str | None = None,
    max_attempts: int = 3,
) -> str:
    """Fetch page via stealth Chromium with retry on transient failures.

    bb.org.bd is reachable from ExonVPS via curl in <0.3s but Playwright
    intermittently sees `ERR_ADDRESS_UNREACHABLE` or hangs on
    `domcontentloaded` during dawn-hour windows. A short per-attempt
    timeout with retries recovers far better than one long single shot.

    Per-attempt timeout default 60s (working runs complete in 11–37s);
    backoff 5s, 10s between attempts. Max budget ~195s for 3 attempts.

    Each attempt launches a fresh browser to avoid carrying corrupt
    state across retries.
    """
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _fetch_once(url, timeout_ms, wait_for_selector)
        except Exception as e:
            last_err = e
            logger.warning(
                "fetch attempt %d/%d failed: %s: %s",
                attempt,
                max_attempts,
                type(e).__name__,
                str(e)[:200],
            )
            if attempt < max_attempts:
                time.sleep(5 * attempt)
    assert last_err is not None
    raise last_err


def parse_exchange_rates(html: str) -> ForexRates:
    """Extract USD + EUR + GBP from BB exchange rates page.

    Table 0 (section.content table:nth-of-type(1)):
        Currency | Bid Rate | Ask Rate | WAR
        USD      | ...      | ...      | ...

    Table 1 (section.content table:nth-of-type(2)):
        Currency | Bid Rate | Ask Rate
        EUR      | ...      | ...
        GBP      | ...      | ...

    Mapping: WAR -> usd_bdt_mid, Bid -> usd_bdt_buy, Ask -> usd_bdt_sell.
    For EUR/GBP cross rates: mid = average of bid and ask; stored in eur_bdt / gbp_bdt.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select("section.content table")
    if len(tables) < 2:
        raise ParseError(
            f"expected 2+ tables in section.content, got {len(tables)}"
        )

    # --- USD table (table 0) ---
    usd_table = tables[0]
    usd_rows = usd_table.find_all("tr")

    usd_bid: float | None = None
    usd_ask: float | None = None
    usd_war: float | None = None

    for row in usd_rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        label = cells[0].get_text(strip=True).upper()
        if label == "USD":
            if len(cells) < 4:
                raise ParseError(
                    f"USD row has {len(cells)} cells — expected 4 (Currency/Bid/Ask/WAR)"
                )
            usd_bid = parse_number(cells[1].get_text(strip=True))
            usd_ask = parse_number(cells[2].get_text(strip=True))
            usd_war = parse_number(cells[3].get_text(strip=True))
            break

    if usd_bid is None or usd_ask is None or usd_war is None:
        raise ParseError("Could not parse USD bid/ask/WAR from exchange rates table")

    # --- Cross rates table (table 1) ---
    cross_table = tables[1]
    cross_rows = cross_table.find_all("tr")

    eur_bdt: float | None = None
    gbp_bdt: float | None = None

    for row in cross_rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        label = cells[0].get_text(strip=True).upper()
        if label == "EUR" and len(cells) >= 3:
            bid = parse_number(cells[1].get_text(strip=True))
            ask = parse_number(cells[2].get_text(strip=True))
            if bid is not None and ask is not None:
                eur_bdt = (bid + ask) / 2.0
        elif label == "GBP" and len(cells) >= 3:
            bid = parse_number(cells[1].get_text(strip=True))
            ask = parse_number(cells[2].get_text(strip=True))
            if bid is not None and ask is not None:
                gbp_bdt = (bid + ask) / 2.0

    if eur_bdt is None:
        raise ParseError("Could not parse EUR/BDT from cross rates table")
    if gbp_bdt is None:
        raise ParseError("Could not parse GBP/BDT from cross rates table")

    return ForexRates(
        usd_bdt_mid=usd_war,
        usd_bdt_buy=usd_bid,
        usd_bdt_sell=usd_ask,
        eur_bdt=eur_bdt,
        gbp_bdt=gbp_bdt,
        source_url="",  # caller sets this via model_copy
    )


def parse_reserves(html: str) -> ForexReserves:
    """Extract gross + BPM6 reserves from BB intreserve page.

    Table: #sortableTable
    Column layout: Period | Foreign Exchange Reserves(Gross) | Foreign Exchange Reserves(as per BPM6)

    The Gross/BPM6 column INDEX is identified by HEADER TEXT, never by fixed
    position — a BB column reorder would otherwise silently swap the two
    figures with no error (the exact failure mode the invariants below exist
    to catch). The header row must be seen before any data row is accepted;
    a table with no recognisable header row raises ParseError rather than
    guessing cells[1]/cells[2].

    Header matching (2026-08-05 review finding H1 -- ambiguity, not just
    absence, must be caught): BPM6 is matched FIRST with a tolerant token
    (``bpm[ -]?6``, case-insensitive -- catches "BPM6"/"BPM 6"/"BPM-6"; the
    previous exact-substring match silently missed the space/dash spellings
    and left bpm6_col=None, quietly disarming the invariant below). Any cell
    matched as BPM6 is EXCLUDED from the Gross scan, which then requires
    "gross" (case-insensitive) to appear in EXACTLY ONE remaining cell.
    Zero or multiple Gross candidates both raise ParseError -- a second
    unrelated column that happens to contain the word "gross" must not
    silently win by virtue of being scanned last (the review reproduced
    exactly this: a stray "gross"-labelled BDT column got selected over the
    real one, and the bpm6<gross direction check alone did not catch it
    because the wrong column's value still happened to exceed BPM6's).

    The table groups rows by fiscal year with a spanning header row.
    The first data row after the header group (row with 2 numeric columns) is the most recent.
    Values are published in millions USD — divide by 1000 to get billions.

    The period label is the month name only (e.g. "March") in the row immediately after
    the fiscal year header (e.g. "2025-2026").  We derive the date from the most recent
    fiscal year header + month name, resolving to the first day of that month.

    Cross-column invariants (both raise ParseError, refusing to write either
    figure -- the caller's existing except-ParseError path already refuses
    the whole run's write, rates included, on any parse failure, closing the
    "column swap reads a corrupt value" hole in AGENTS.md landmine 38):

      1. Direction: BPM6 reserves are always BELOW gross reserves (BB's BPM6
         methodology excludes items gross includes). bpm6 >= gross means the
         columns were misidentified (e.g. an unannounced reorder).
      2. Magnitude band (2026-08-05 review finding H2, memo §3.3): even with
         the direction right, bpm6/gross must fall in [0.70, 0.95] (see
         ``_BPM6_GROSS_RATIO_MIN/MAX`` for how that band was calibrated
         against real history). This catches a same-direction magnitude/unit
         corruption on just one column (e.g. a decimal or million/billion
         mixup) that the direction check alone cannot see.

    import_cover_months is NOT published on this page — set to None.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#sortableTable")
    if table is None:
        raise ParseError("table#sortableTable not found")

    rows = table.find_all("tr")

    # Skip header rows until we find the first fiscal year group
    # Structure:
    #   row: [(In million US $)]
    #   row: [Period, Gross, BPM6]   <- column headers (index captured by TEXT)
    #   row: [2025-2026]              <- fiscal year header (colspan)
    #   row: [March, 34116.6, 29501.2] <- data
    #   row: [February, ...]

    current_year: str | None = None
    most_recent_month: str | None = None
    most_recent_gross_mn: float | None = None
    most_recent_bpm6_mn: float | None = None
    gross_col: int | None = None
    bpm6_col: int | None = None
    header_row_seen = False

    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        if len(cells) == 1:
            text = cells[0].get_text(strip=True)
            # Fiscal year header looks like "2025-2026"; other 1-cell rows
            # are noise like "(In million US $)" and are skipped.
            if _FISCAL_YEAR_RE.match(text):
                current_year = text
            continue

        if len(cells) < 3:
            continue

        label = cells[0].get_text(strip=True)

        if label.strip().lower() in _RESERVES_PERIOD_HEADER_LABELS:
            # Column-header row -- identify Gross / BPM6 by TEXT, not
            # position. BPM6 matched first (tolerant token) and excluded
            # from the Gross scan; Gross must match exactly once.
            bpm6_candidates: list[int] = []
            gross_candidates: list[int] = []
            header_texts: list[str] = []
            for i, cell in enumerate(cells):
                header_text = cell.get_text(strip=True)
                header_texts.append(header_text)
                if _BPM6_HEADER_RE.search(header_text):
                    bpm6_candidates.append(i)
                    continue
                if "gross" in header_text.lower():
                    gross_candidates.append(i)

            if len(gross_candidates) != 1:
                raise ParseError(
                    "header identification failed: expected exactly one "
                    f"column matching 'gross', found {len(gross_candidates)} "
                    f"in header row {header_texts!r}"
                )
            if len(bpm6_candidates) > 1:
                raise ParseError(
                    "header identification failed: expected at most one "
                    f"column matching a BPM6 pattern, found "
                    f"{len(bpm6_candidates)} in header row {header_texts!r}"
                )

            gross_col = gross_candidates[0]
            bpm6_col = bpm6_candidates[0] if bpm6_candidates else None
            header_row_seen = True
            continue

        if gross_col is None:
            # Haven't seen a usable header row yet -- can't trust position.
            continue

        gross = parse_number(cells[gross_col].get_text(strip=True))
        if gross is None:
            continue
        bpm6 = (
            parse_number(cells[bpm6_col].get_text(strip=True))
            if bpm6_col is not None
            else None
        )

        most_recent_month = label
        most_recent_gross_mn = gross
        most_recent_bpm6_mn = bpm6
        break  # First data row is most recent

    if most_recent_gross_mn is None or most_recent_month is None:
        if not header_row_seen:
            raise ParseError(
                "header identification failed: no Period/Month/End Period "
                "header row with a recognisable Gross column was found in "
                "#sortableTable"
            )
        raise ParseError("Could not find any reserves data rows in #sortableTable")

    gross_bn = most_recent_gross_mn / 1000.0
    bpm6_bn = most_recent_bpm6_mn / 1000.0 if most_recent_bpm6_mn is not None else None

    if bpm6_bn is not None:
        if bpm6_bn >= gross_bn:
            raise ParseError(
                f"BPM6 reserves ({bpm6_bn:.4f}bn) >= Gross reserves ({gross_bn:.4f}bn) "
                f"for {most_recent_month!r} -- BPM6 must be strictly below Gross by "
                "construction, so this looks like a Gross/BPM6 column identification "
                "failure; refusing to write either figure"
            )
        ratio = bpm6_bn / gross_bn
        if not (_BPM6_GROSS_RATIO_MIN <= ratio <= _BPM6_GROSS_RATIO_MAX):
            raise ParseError(
                f"BPM6/Gross ratio {ratio:.4f} for {most_recent_month!r} is outside "
                f"the expected band [{_BPM6_GROSS_RATIO_MIN}, {_BPM6_GROSS_RATIO_MAX}] "
                f"(bpm6={bpm6_bn:.4f}bn, gross={gross_bn:.4f}bn) -- this looks like "
                "magnitude/unit corruption on one column (e.g. a decimal or "
                "million/billion mixup), not a column swap; refusing to write "
                "either figure"
            )

    # Derive reserves_date: first of month, in current_year (second half = calendar year of end)
    reserves_date = _parse_reserves_date(most_recent_month, current_year)

    return ForexReserves(
        gross_reserves_usd_bn=gross_bn,
        bpm6_reserves_usd_bn=bpm6_bn,
        import_cover_months=None,
        reserves_date=reserves_date,
        source_url="",  # caller sets this via model_copy
    )


def _parse_reserves_date(
    month_name: str, fiscal_year: str | None, today: date | None = None
) -> date:
    """Derive a date from month name and fiscal year string like '2025-2026'.

    BD fiscal year runs July-June. Month names are English (January, February, etc.).
    July-December fall in the fiscal year's start (calendar) year; January-June fall
    in its end (calendar) year.

    When no fiscal-year header was captured (or it doesn't match the expected
    "YYYY-YYYY" shape), fall back to today's year — but guard the January
    window: if that fallback lands in the future, the row's month must belong
    to last year, not this one (e.g. today is 2027-01-05 and the most recent
    row read is "November" with no header in view — that is November 2026,
    not a November that hasn't happened yet).

    An unrecognised period label (e.g. BB relabels "March" to "Mar-2026")
    raises ParseError rather than fabricating today's month — silently
    guessing a date here would accept whatever value came with that row with
    no alert, which is worse than failing loudly.

    Args:
        today: Injectable "current date" for deterministic tests. Defaults to
            date.today().

    Raises:
        ParseError: month_name doesn't match a known English month name.
    """
    today = today or date.today()

    month_map = {
        m.lower(): i for i, m in enumerate(
            ["", "january", "february", "march", "april", "may", "june",
             "july", "august", "september", "october", "november", "december"]
        ) if m
    }

    month_num = month_map.get(month_name.lower().strip())
    if month_num is None:
        raise ParseError(
            f"unrecognised reserves period label {month_name!r} — "
            "BB's reserves table layout may have changed"
        )

    if fiscal_year:
        match = _FISCAL_YEAR_RE.match(fiscal_year)
        if match:
            # fiscal_year like "2025-2026": first half (Jul-Dec) = start year,
            # second half (Jan-Jun) = end year.
            start_year, end_year = int(match.group(1)), int(match.group(2))
            year = end_year if month_num <= 6 else start_year
            return date(year, month_num, 1)

    # No usable fiscal header — fall back to today's year, with the
    # future-date guard described above.
    candidate = date(today.year, month_num, 1)
    if candidate > today:
        candidate = date(today.year - 1, month_num, 1)
    return candidate


def load_previous_snapshot(today: date) -> ForexSnapshot | None:
    """Find and load the most recent snapshot file strictly older than today."""
    if not DATA_DIR.exists():
        return None

    candidates = sorted(DATA_DIR.glob("????-??-??.json"), reverse=True)
    for path in candidates:
        try:
            file_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if file_date < today:
            try:
                with path.open(encoding="utf-8") as fh:
                    raw = json.load(fh)
                return ForexSnapshot.model_validate(raw)
            except Exception as exc:
                logger.warning("Failed to load previous snapshot %s: %s", path, exc)
                continue
    return None


def write_snapshot(snapshot: ForexSnapshot) -> Path:
    """Atomically write snapshot JSON to DATA_DIR/YYYY-MM-DD.json.

    Uses a .tmp file + os.replace for atomic rename so interrupted writes
    never leave a partial file at the final path.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    final_path = DATA_DIR / f"{snapshot.date.isoformat()}.json"
    tmp_path = DATA_DIR / f"{snapshot.date.isoformat()}.json.tmp"

    json_bytes = snapshot.model_dump_json(indent=2)
    tmp_path.write_text(json_bytes, encoding="utf-8")
    os.replace(tmp_path, final_path)
    return final_path


def main() -> int:
    """Fetch, validate, anomaly-check, and write a ForexSnapshot.

    Reserves are published MONTHLY, not daily, so they are gated on month
    advance rather than the rates' fractional-change band — see the reserves
    section below for the month-advance / same-month-revision / month-regressed
    cases.

    Exit codes:
        0 — success, snapshot written with fresh rates and fresh (or
            month-advanced) reserves
        1 — fetch / parse / validation error
        2 — anomaly: a rate anomaly skips the write entirely; a reserves
            same-month revision anomaly or month regression HOLDS reserves
            (the previous reserves value is carried forward verbatim) but
            still writes the snapshot with fresh rates
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with CONFIG_PATH.open() as f:
        sources = json.load(f)["sources"]
    rates_url = sources["bb_exchange_rates"]["url"]
    reserves_url = sources["bb_forex_reserves"]["url"]

    thresholds = load_thresholds(THRESHOLDS_PATH)

    try:
        logger.info("Fetching exchange rates from %s", rates_url)
        rates_html = fetch_rendered_html(
            rates_url, wait_for_selector="section.content table"
        )
        rates = parse_exchange_rates(rates_html)
        rates = rates.model_copy(update={"source_url": rates_url})

        logger.info("Fetching reserves from %s", reserves_url)
        reserves_html = fetch_rendered_html(
            reserves_url, wait_for_selector="table#sortableTable"
        )
        reserves = parse_reserves(reserves_html)
        reserves = reserves.model_copy(update={"source_url": reserves_url})
    except ParseError as e:
        logger.exception("bb_forex parse failed")
        notify(
            "error",
            "bb_forex parse failed",
            f"{type(e).__name__}: {e} — BB page layout may have changed",
        )
        return 1
    except Exception as e:
        logger.exception("bb_forex fetch failed")
        notify("error", "bb_forex fetch failed", f"{type(e).__name__}: {e}")
        return 1

    # Anomaly check vs previous snapshot
    prev = load_previous_snapshot(date.today())
    prev_res = prev.reserves if prev is not None else None
    reserves_for_snapshot = reserves
    exit_code = 0

    if prev is not None:
        rate_checks = [
            ("usd_bdt_mid", rates.usd_bdt_mid, prev.rates.usd_bdt_mid),
            ("usd_bdt_buy", rates.usd_bdt_buy, prev.rates.usd_bdt_buy),
            ("usd_bdt_sell", rates.usd_bdt_sell, prev.rates.usd_bdt_sell),
            ("eur_bdt", rates.eur_bdt, prev.rates.eur_bdt),
            ("gbp_bdt", rates.gbp_bdt, prev.rates.gbp_bdt),
        ]
        for metric, new, old in rate_checks:
            ok, pct = check_threshold(metric, new, old, thresholds)
            if not ok:
                notify(
                    "warning",
                    "bb_forex anomaly — write skipped",
                    f"{metric}: {old} -> {new} ({pct:.2%} exceeds threshold)",
                )
                return 2

        # Reserves are published MONTHLY (see BB's #sortableTable), unlike the
        # daily rates above — a >3% step is the expected shape of a normal
        # month-to-month move, not an anomaly. Gate on reserves_date instead:
        #   - month advanced  -> accept the new value regardless of magnitude
        #   - same month      -> apply the existing fractional-change band;
        #                        a trip HOLDS the previous value (revision
        #                        noise / mid-cycle re-read), doesn't reject
        #                        the whole run
        #   - month regressed -> HOLD; this is the wrong-column/layout
        #                        signature (e.g. reading the BPM6 column)
        if prev_res is not None:
            if reserves.reserves_date > prev_res.reserves_date:
                # Accepted regardless of magnitude -- reserves genuinely move
                # in month-sized jumps, including large POSITIVE ones: BD's
                # fiscal year ends in June, and June reserves spike hard
                # (this repo's own fixture history: May->June 2025 = +23.16%,
                # May->June 2024 = +10.40%, both entirely legitimate). A
                # wrong-VALUE-column read (e.g. landing on BPM6 instead of
                # gross, which reads noticeably lower for the same period --
                # observed at -12.4% / -15.3%) can land on a fresh month too,
                # since the month label and the value live in different table
                # cells -- the date alone can't catch it.
                #
                # The split between the two is DIRECTIONAL, not a shared
                # magnitude cutoff: legitimate large steps are positive
                # (June spikes), wrong-column reads are negative. The
                # largest legitimate NEGATIVE step observed (a fiscal
                # year-end June -> July rollover) is only -6.21%. Because a
                # June spike and a column swap can overlap in raw magnitude,
                # this check stays warn-only -- it must never HOLD an
                # advance, only flag one for a human to check.
                drop_limit = thresholds.get(
                    "gross_reserves_usd_bn_advance_drop", _RESERVES_ADVANCE_DROP_DEFAULT
                )
                jump_limit = thresholds.get(
                    "gross_reserves_usd_bn_advance_jump", _RESERVES_ADVANCE_JUMP_DEFAULT
                )
                if prev_res.gross_reserves_usd_bn:
                    signed_pct = (
                        reserves.gross_reserves_usd_bn - prev_res.gross_reserves_usd_bn
                    ) / prev_res.gross_reserves_usd_bn
                else:
                    signed_pct = 0.0
                logger.info(
                    "reserves month advanced %s -> %s (%.4fbn -> %.4fbn, %+.2f%% step)",
                    prev_res.reserves_date,
                    reserves.reserves_date,
                    prev_res.gross_reserves_usd_bn,
                    reserves.gross_reserves_usd_bn,
                    signed_pct * 100,
                )
                if signed_pct < -drop_limit or signed_pct > jump_limit:
                    notify(
                        "warning",
                        "bb_forex reserves month-advance step unusually large",
                        (
                            f"gross_reserves {prev_res.reserves_date.isoformat()} -> "
                            f"{reserves.reserves_date.isoformat()}: "
                            f"{prev_res.gross_reserves_usd_bn:.2f}bn -> "
                            f"{reserves.gross_reserves_usd_bn:.2f}bn ({signed_pct:+.2%}) "
                            f"is outside [-{drop_limit:.0%}, +{jump_limit:.0%}] "
                            "— verify this isn't a wrong-column read (e.g. BPM6 vs gross)"
                        ),
                    )
                # Still accepted either way -- reserves_for_snapshot stays
                # the new value, exit_code stays 0.
            elif reserves.reserves_date == prev_res.reserves_date:
                ok, pct = check_threshold(
                    "gross_reserves_usd_bn",
                    reserves.gross_reserves_usd_bn,
                    prev_res.gross_reserves_usd_bn,
                    thresholds,
                )
                if not ok:
                    notify(
                        "warning",
                        "bb_forex reserves same-month revision anomaly — value held",
                        (
                            f"gross_reserves at {reserves.reserves_date.isoformat()}: "
                            f"{prev_res.gross_reserves_usd_bn:.2f}bn -> "
                            f"{reserves.gross_reserves_usd_bn:.2f}bn ({pct:.2%})"
                        ),
                    )
                    reserves_for_snapshot = prev_res
                    exit_code = 2
            else:
                notify(
                    "warning",
                    "bb_forex reserves month regressed — value held",
                    (
                        f"reserves_date regressed: {prev_res.reserves_date.isoformat()} "
                        f"-> {reserves.reserves_date.isoformat()}"
                    ),
                )
                reserves_for_snapshot = prev_res
                exit_code = 2

    snapshot = ForexSnapshot(
        schema_version="1.0",
        date=date.today(),
        scraped_at=datetime.now(timezone.utc),
        rates=rates,
        reserves=reserves_for_snapshot,
    )
    path = write_snapshot(snapshot)
    logger.info("wrote %s", path)
    return exit_code


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("bb_forex", "econdelta-forex.service", main))
