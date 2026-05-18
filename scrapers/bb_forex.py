"""BB forex + reserves scraper — Playwright-driven, anomaly-gated, schema-validated."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from utils.anomaly import check_threshold, load_thresholds
from utils.notifier import notify
from utils.parser import parse_number
from utils.schema import ForexRates, ForexReserves, ForexSnapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "bb_forex"
CONFIG_PATH = REPO_ROOT / "config" / "sources.json"
THRESHOLDS_PATH = REPO_ROOT / "config" / "thresholds.json"

logger = logging.getLogger("bb_forex")


class ParseError(Exception):
    """Raised when page HTML does not match the expected structure."""


def _is_captcha_page(html: str) -> bool:
    """Detect BB's image-CAPTCHA wall.

    BB serves a CAPTCHA challenge to flagged IPs (e.g. data-center addresses
    like ExonVPS). The wall contains an "answer" input, a "jar" submit button,
    a thumbnail image to identify, and a "support ID" footer. All four markers
    must be present — any one alone could be a false positive.
    """
    markers = ('id="ans"', 'id="jar"', 'class="thumbnails"', "support ID")
    return all(m in html for m in markers)


# BB renders the challenge image as either:
#   <img ... class="thumbnails" ... src="data:image/png;base64,...">  (plan order)
#   <img ... src="data:image/png;base64,..." ... class="thumbnails">  (live fixture order)
# We accept both orderings, anchored on class="thumbnails" so we don't
# accidentally match the unrelated red-dot / audio-icon images on the page.
_CAPTCHA_IMG_RE = re.compile(
    r'<img[^>]+(?:'
    r'class="thumbnails"[^>]+src="data:image/png;base64,([^"]+)"'
    r'|'
    r'src="data:image/png;base64,([^"]+)"[^>]+class="thumbnails"'
    r')',
    re.IGNORECASE,
)


def _extract_captcha_image(html: str, dest_path: Path) -> None:
    """Extract the base64-encoded captcha PNG from BB's captcha-wall HTML.

    BB embeds the challenge image as a data URI on an <img class="thumbnails">
    tag. We decode and write atomically (tmp + rename), mirroring the
    write_snapshot() pattern in this module.
    """
    m = _CAPTCHA_IMG_RE.search(html)
    if m is None:
        raise ParseError("no captcha image found in captcha-page HTML")
    b64 = m.group(1) or m.group(2)
    try:
        png_bytes = base64.b64decode(b64)
    except Exception as e:
        raise ParseError(f"failed to decode captcha image base64: {e}") from e

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    tmp_path.write_bytes(png_bytes)
    os.replace(tmp_path, dest_path)


_CAPTCHA_SOLVE_TIMEOUT_S = 60
_CAPTCHA_SOLVE_MAX_ANSWER_LEN = 30
_CAPTCHA_SOLVE_MAX_ATTEMPTS = 3
_CAPTCHA_SOLVE_PROMPT = (
    "What single common object is shown in this image? "
    "Examples of valid answers: 'bottle', 'arrows', 'dot', 'apple'. "
    "Reply with ONLY a single English lowercase common noun, no other text."
)


def _solve_captcha_via_claude(image_path: Path) -> str | None:
    """Identify the object in a BB captcha image via Claude vision.

    Returns the predicted single-word answer (lowercase, no punctuation), or
    None on any failure (timeout, non-zero exit, empty output, over-long
    output). Caller wraps with retry.

    Uses `claude -p` with the image attached via @filepath syntax —
    Claude Code's prompt-side file reference triggers vision-mode
    attachment. Model is claude-haiku-4-5 (cheapest multimodal, sufficient
    for the simple object-identification task BB asks).

    Auth via CLAUDE_CODE_OAUTH_TOKEN env var (set in /etc/econdelta.env).
    """
    binary = os.environ.get("CLAUDE_BINARY", "claude")
    prompt_with_image = f"{_CAPTCHA_SOLVE_PROMPT}\n\n@{image_path}"
    argv = [
        binary, "--print", "--strict-mcp-config",
        "--model", "claude-haiku-4-5",
        "--no-session-persistence",
        "--tools", "",
        "--permission-mode", "bypassPermissions",
        prompt_with_image,
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_CAPTCHA_SOLVE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None

    raw = result.stdout.strip().lower()
    if not raw:
        return None

    first_word = raw.split()[0].rstrip(".,!?;:'\"")
    if not first_word or len(first_word) > _CAPTCHA_SOLVE_MAX_ANSWER_LEN:
        return None

    return first_word


def _solve_captcha_loop(page, html: str, timeout_ms: int) -> str:
    """Drive BB's CAPTCHA challenge until cleared, or fail after 3 attempts.

    Refactored out of `_fetch_once` so the captcha-handling logic can be
    tested in isolation with a fake page stub — mocking the entire
    sync_playwright() context manager chain would be brittle.

    The caller passes the initial HTML (from page.content() after page.goto)
    so we can short-circuit when no captcha is present.

    Loop body: extract challenge PNG to a temp file → ask Claude what object
    is shown → fill #ans with the answer → click #jar → wait for navigation
    → re-read page.content(). If the new HTML is still a captcha page, retry.

    Returns the final non-captcha HTML. Raises ParseError if 3 attempts pass
    without clearing.
    """
    for attempt in range(1, _CAPTCHA_SOLVE_MAX_ATTEMPTS + 1):
        if not _is_captcha_page(html):
            return html
        logger.info(
            "BB captcha detected (attempt %d/%d)",
            attempt,
            _CAPTCHA_SOLVE_MAX_ATTEMPTS,
        )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tmp_path = Path(tf.name)
        try:
            _extract_captcha_image(html, tmp_path)
            answer = _solve_captcha_via_claude(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        if answer is None:
            logger.warning("captcha solver returned None on attempt %d", attempt)
            continue
        logger.info("captcha solver returned %r — submitting", answer)
        page.fill("#ans", answer)
        page.click("#jar")
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        html = page.content()
    raise ParseError("captcha solve failed after 3 attempts")


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
            # _is_captcha_page docstring for marker logic.
            html = page.content()
            html = _solve_captcha_loop(page, html, timeout_ms)

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
    """Extract gross reserves from BB intreserve page.

    Table: #sortableTable
    Column layout: Period | Foreign Exchange Reserves(Gross) | Foreign Exchange Reserves(as per BPM6)

    The table groups rows by fiscal year with a spanning header row.
    The first data row after the header group (row with 2 numeric columns) is the most recent.
    Values are published in millions USD — divide by 1000 to get billions.

    The period label is the month name only (e.g. "March") in the row immediately after
    the fiscal year header (e.g. "2025-2026").  We derive the date from the most recent
    fiscal year header + month name, resolving to the first day of that month.

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
    #   row: [Period, Gross, BPM6]   <- column headers
    #   row: [2025-2026]              <- fiscal year header (colspan)
    #   row: [March, 34116.6, 29501.2] <- data
    #   row: [February, ...]

    current_year: str | None = None
    most_recent_month: str | None = None
    most_recent_gross_mn: float | None = None

    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        if len(cells) == 1:
            text = cells[0].get_text(strip=True)
            # Fiscal year header looks like "2025-2026" or "(In million US $)"
            if "-" in text and text.replace("-", "").replace(" ", "").isdigit() is False:
                # Could be fiscal year like "2025-2026"
                parts = text.split("-")
                if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
                    current_year = text.strip()
            continue

        if len(cells) >= 3:
            label = cells[0].get_text(strip=True)
            gross_str = cells[1].get_text(strip=True)
            gross = parse_number(gross_str)

            if gross is not None and label not in ("Period", "Foreign Exchange Reserves(Gross)"):
                # This is a data row
                if most_recent_month is None:
                    most_recent_month = label
                    most_recent_gross_mn = gross
                    break  # First data row is most recent

    if most_recent_gross_mn is None or most_recent_month is None:
        raise ParseError("Could not find any reserves data rows in #sortableTable")

    gross_bn = most_recent_gross_mn / 1000.0

    # Derive reserves_date: first of month, in current_year (second half = calendar year of end)
    reserves_date = _parse_reserves_date(most_recent_month, current_year)

    return ForexReserves(
        gross_reserves_usd_bn=gross_bn,
        import_cover_months=None,
        reserves_date=reserves_date,
        source_url="",  # caller sets this via model_copy
    )


def _parse_reserves_date(month_name: str, fiscal_year: str | None) -> date:
    """Derive a date from month name and fiscal year string like '2025-2026'.

    BD fiscal year runs July–June. Month names are English (January, February, etc.).
    We determine the calendar year from which half of the fiscal year the month falls in.
    If fiscal_year is unknown we use the current year.
    """
    import calendar as cal

    month_map = {
        m.lower(): i for i, m in enumerate(
            ["", "january", "february", "march", "april", "may", "june",
             "july", "august", "september", "october", "november", "december"]
        ) if m
    }

    month_num = month_map.get(month_name.lower().strip())
    if month_num is None:
        # Unrecognised month — fall back to first of current month
        today = date.today()
        return date(today.year, today.month, 1)

    if fiscal_year:
        # fiscal_year like "2025-2026": first half (Jul-Dec) = start year, second half (Jan-Jun) = end year
        parts = fiscal_year.split("-")
        try:
            start_year = int(parts[0].strip())
            end_year = int(parts[1].strip())
        except (ValueError, IndexError):
            start_year = date.today().year
            end_year = start_year
        year = end_year if month_num <= 6 else start_year
    else:
        year = date.today().year

    return date(year, month_num, 1)


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

    Exit codes:
        0 — success, snapshot written
        1 — fetch / parse / validation error
        2 — anomaly detected, write skipped
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
    except (ParseError, Exception) as e:
        logger.exception("fetch/parse failed")
        notify("error", "bb_forex fetch failed", f"{type(e).__name__}: {e}")
        return 1

    # Anomaly check vs previous snapshot
    prev = load_previous_snapshot(date.today())
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

        if prev.reserves is not None:
            ok, pct = check_threshold(
                "gross_reserves_usd_bn",
                reserves.gross_reserves_usd_bn,
                prev.reserves.gross_reserves_usd_bn,
                thresholds,
            )
            if not ok:
                notify(
                    "warning",
                    "bb_forex reserves anomaly — write skipped",
                    (
                        f"gross_reserves: {prev.reserves.gross_reserves_usd_bn:.2f}bn "
                        f"-> {reserves.gross_reserves_usd_bn:.2f}bn ({pct:.2%})"
                    ),
                )
                return 2

    snapshot = ForexSnapshot(
        schema_version="1.0",
        date=date.today(),
        scraped_at=datetime.now(timezone.utc),
        rates=rates,
        reserves=reserves,
    )
    path = write_snapshot(snapshot)
    logger.info("wrote %s", path)
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("bb_forex", "econdelta-forex.service", main))
