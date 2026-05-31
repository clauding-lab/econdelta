"""Bangladesh Bank primary-auction scraper — per-tenor RESULTS + forward CALENDAR.

Feeds the two structured row-tables created in S8 (``auction_results`` /
``auction_calendar``), which exist precisely because ``metric_history`` cannot
hold row-shaped data: it is scalar-numeric-only and the writer drops dict/list
payloads (``utils/supabase_writer._rows_from_data``). An auction print is a
multi-row, multi-field record, so it goes through S8's dedicated structured
writers (``upsert_auction_results`` / ``upsert_auction_calendar``), NOT through
the scalar fetch_all / parse_all / aggregate pipeline.

Why a ``scrapers/`` one-shot (mirroring ``scrapers/imf_eff.py``) and NOT a
``sources-v3.json`` config indicator + registered parser:

  - A config indicator runs through ``parse_one`` -> ``ParseResult`` -> the
    scalar aggregate, whose value is a single ``float|int|str|dict`` and whose
    only sinks are ``metric_history`` (scalar) and ``_flatten_dict_indicators``
    (dict fan-out into MORE scalars). There is no path from that pipeline into a
    structured row-table — S8 deliberately built the auction writers as
    standalone functions, not wired into aggregate. A standalone scraper that
    calls them directly is the ONLY route to the new tables.
  - Because this scraper does NOT use the ``parse.deterministic`` REGISTRY,
    landmine A (forgetting ``import parsers.<name>`` in ``parse_all.py``) does
    NOT apply here: the parse helpers below are plain module functions called
    directly, like ``scrapers/imf_eff.parse_eff_outstanding``. There is no
    ``@register`` decorator to silently no-op.

TWO sources, ONE scraper:

  RESULTS (``auction_results``) — per-print results for auctions that HAVE
    happened. BB publishes one auction-result press release per business day
    under ``mediaroom/press_release_details/rrpt/<id>``; the SAME ``rrpt``
    discovery as S7 (``fetchers.rrpt_discovery``) finds the latest one. The
    release carries, per tenor, the accepted SIZE, total BID, the BID-COVER
    ratio, the weighted-average MATURITY (WAM, bonds), and the CUTOFF /
    weighted-average yield. CUTOFF is already captured as scalars
    (``bill_bond_rates`` / ``tbill_*_yield`` / ``tbond_*_yield``); the NEW
    fields are size/bid/cover/wam, stored per-tenor as rows.

  CALENDAR (``auction_calendar``) — the forward weekly ISSUANCE strip. EconDelta
    already hits ``monetaryactivity/auc_calendar`` as the scalar ``gsec_auction``
    (the topmost notional only); that scalar indicator stays UNTOUCHED for its
    existing consumer. This scraper re-fetches the same calendar page and emits
    ALL future weekly per-tenor rows (a 12-week forward strip), each
    ``{auction_date, tenor, notional}``.

BD EGRESS (CAPTCHA wall confirmed): BB firewalls non-BD IPs, so the live fetch +
parse of BOTH sources is VPS-deferred (ExonVPS Dhaka, where the cron runs). The
parse helpers are PURE (operate on captured HTML text) so they unit-test fully
offline against a synthetic fixture; the live fetch is what the VPS run confirms
(page shape, row labels, column order, date format).

INTERMITTENT: not every business day holds every tenor (T-bills weekly, BGTB
less often; BB has thinned routine issuance). A tenor that is absent from a
release/calendar simply yields no row — never a fabricated 0 and never a stale
carry-forward (the structured tables have no "last good" fallback).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from claude_max.max_client import MaxCallError, run_max
from fetchers.rrpt_discovery import discover_latest_rrpt_link
from utils.notifier import notify
from utils.supabase_writer import (
    SupabaseWriteError,
    upsert_auction_calendar,
    upsert_auction_results,
)

logger = logging.getLogger("bb_auction")

# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

# RESULTS: the BB press-release LISTING page; ``discover_latest_rrpt_link``
# walks it for the highest-numbered /rrpt/<id> auction-result notice (S7's
# discovery, reused). ``title_pattern`` keeps only auction-result notices.
PRESS_RELEASE_LISTING_URL = "https://www.bb.org.bd/en/index.php/mediaroom/press_release"
RESULTS_TITLE_PATTERN = "Result of the Auction"

# CALENDAR: the forward auction calendar (same page the scalar gsec_auction hits).
AUCTION_CALENDAR_URL = "https://www.bb.org.bd/en/index.php/monetaryactivity/auc_calendar"

_TIMEOUT = 30

# How many forward weeks of the calendar to keep. The horizon BB firms up varies
# (sometimes only 4-8 weeks), so this is a CEILING, not a guarantee — fewer rows
# is normal, not an error.
CALENDAR_HORIZON_WEEKS = 12

# Canonical tenor labels (match the existing scalar ids tbill_182d_yield /
# tbond_5y_yield and the migration's column comments).
_TBILL_TENORS = {"91": "91d", "182": "182d", "364": "364d"}
_TBOND_TENORS = {"2": "2y", "5": "5y", "10": "10y", "15": "15y", "20": "20y"}

# Validation bands (reject a parse that grabbed the wrong cell).
_SIZE_RANGE = (0.0, 100000.0)      # accepted amount, BDT crore
_BID_RANGE = (0.0, 500000.0)       # total bid, BDT crore
_COVER_RANGE = (0.0, 50.0)         # bid-to-cover ratio
_WAM_RANGE = (0.0, 40.0)           # weighted-average maturity, years
_CUTOFF_RANGE = (0.0, 25.0)        # cut-off / weighted-average yield, percent
_NOTIONAL_RANGE = (0.0, 100000.0)  # forward notional, BDT crore

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "claude_max" / "prompts"


class FetchError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

_AMOUNT_RE = re.compile(r"-?\d[\d,]*\.?\d*")
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def _to_number(text: str) -> float | None:
    cleaned = (text or "").replace(",", "").strip()
    if not cleaned or not re.search(r"\d", cleaned):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _in_range(value: float | None, bounds: tuple[float, float]) -> float | None:
    """Return value if within bounds, else None (a parse that grabbed a wrong cell)."""
    if value is None:
        return None
    lo, hi = bounds
    return value if lo <= value <= hi else None


def _parse_date_token(raw: str) -> date | None:
    """Parse '28 May, 2025' / 'May 28, 2025' / '28/05/2025' / '2025-05-28' to a date."""
    raw = raw.strip()
    iso = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    dmy = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", raw)
    if dmy:
        d, m, y = (int(dmy.group(i)) for i in (1, 2, 3))
        if y < 100:
            y += 2000
        try:
            return date(y, m, d)
        except ValueError:
            return None
    tokens = re.findall(r"[A-Za-z]+|\d+", raw)
    month = day = year = None
    for t in tokens:
        tl = t.lower()
        if tl in _MONTHS:
            month = _MONTHS[tl]
        elif t.isdigit():
            n = int(t)
            if n > 31:
                year = n
            elif day is None:
                day = n
            else:
                year = n
    if month and day and year:
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _tenor_label(text: str) -> str | None:
    """Map a free-text instrument cell to a canonical tenor label, or None.

    Header-LABEL matching (landmine E): keys off the tenor words / day-counts in
    the cell text, NOT a fixed column index. '91-Day T-Bill' -> '91d';
    '5-Year BGTB' / '5 Yr T-Bond' -> '5y'. Also accepts an already-canonical
    label ('182d', '5y') so the LLM-coerce path can re-validate its own output.
    """
    t = text.lower().strip()
    # Already-canonical labels (the LLM is asked to emit these; coerce re-checks).
    if t in set(_TBILL_TENORS.values()) | set(_TBOND_TENORS.values()):
        return t
    # Day-count tenors -> T-bill labels ('91-day', '182 day').
    m = re.search(r"\b(\d{2,3})\s*-?\s*day", t)
    if m and m.group(1) in _TBILL_TENORS:
        return _TBILL_TENORS[m.group(1)]
    # Year tenors -> bond labels ('5-year', '5 yr', '5y').
    m = re.search(r"\b(\d{1,2})\s*-?\s*(?:year|yr|y)\b", t)
    if m and m.group(1) in _TBOND_TENORS:
        return _TBOND_TENORS[m.group(1)]
    return None


# --------------------------------------------------------------------------- #
# RESULTS — per-tenor row extraction from the auction-result press release
# --------------------------------------------------------------------------- #

_HELD_ON_RE = re.compile(
    r"held\s+on\s+([0-9]{1,2}\s+[A-Za-z]+,?\s+[0-9]{4}"
    r"|[A-Za-z]+\s+[0-9]{1,2},?\s+[0-9]{4})",
    re.IGNORECASE,
)
# Column-header synonyms -> the canonical auction_results field. Header-LABEL
# matching: we locate each field's column by its header text, not a fixed index,
# because BB column order drifts and not every release carries every column.
_RESULT_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "size": ("accepted", "allotted", "allotment", "issued", "awarded", "accepted amount"),
    "bid": ("bid amount", "total bid", "bid received", "tendered", "bids received"),
    "cover": ("cover", "bid-cover", "bid cover", "bid-to-cover", "times"),
    "wam": ("wam", "maturity"),
    "cutoff": ("cut-off", "cutoff", "cut off", "weighted average yield",
               "weighted-average yield", "yield", "rate"),
}
_RESULT_FIELD_RANGE = {
    "size": _SIZE_RANGE, "bid": _BID_RANGE, "cover": _COVER_RANGE,
    "wam": _WAM_RANGE, "cutoff": _CUTOFF_RANGE,
}


def recover_held_on(html_text: str) -> date | None:
    """Pull the auction date from the 'held on <date>' press-release title."""
    flat = re.sub(r"\s+", " ", html_text)
    m = _HELD_ON_RE.search(flat)
    return _parse_date_token(m.group(1)) if m else None


def _header_field_map(header_cells: list[str]) -> dict[int, str]:
    """Map column index -> canonical field by matching the header row labels."""
    mapping: dict[int, str] = {}
    for idx, raw in enumerate(header_cells):
        h = re.sub(r"\s+", " ", raw).strip().lower()
        for field, synonyms in _RESULT_HEADER_SYNONYMS.items():
            if field in mapping.values():
                continue
            if any(syn in h for syn in synonyms):
                mapping[idx] = field
                break
    return mapping


def parse_auction_results(
    html_text: str, *, auction_date: date | None = None,
) -> list[dict]:
    """Extract per-tenor RESULTS rows from a BB auction-result press release.

    Pure (no I/O) so it unit-tests offline against a captured fixture. Returns a
    list of ``{auction_date, tenor, size?, bid?, cover?, wam?, cutoff?}`` dicts —
    one per tenor present, with only the fields the release actually carries.
    An absent tenor yields NO row (never a fabricated 0).

    Deterministic table parse: find the results ``<table>``, read its header row
    to map columns -> fields by LABEL (landmine E), then read each data row whose
    first cell names a known tenor. Returns ``[]`` when no parseable results
    table is found (the caller then falls back to the LLM extract).
    """
    auction_date = auction_date or recover_held_on(html_text)
    soup = BeautifulSoup(html_text, "html.parser")
    rows: list[dict] = []

    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        header_cells = [c.get_text(" ", strip=True) for c in trs[0].find_all(["td", "th"])]
        field_map = _header_field_map(header_cells)
        if not field_map:
            continue  # not a results table (no recognised result columns)
        for tr in trs[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            tenor = _tenor_label(cells[0])
            if tenor is None:
                continue
            row: dict = {"tenor": tenor}
            if auction_date is not None:
                row["auction_date"] = auction_date
            for idx, field in field_map.items():
                if idx < len(cells):
                    val = _in_range(_to_number(cells[idx]), _RESULT_FIELD_RANGE[field])
                    if val is not None:
                        row[field] = val
            # Keep the row only if it carried at least one real field beyond the PK.
            if any(k in row for k in _RESULT_FIELD_RANGE):
                rows.append(row)

    return rows


# --------------------------------------------------------------------------- #
# CALENDAR — forward per-tenor issuance strip from the auction calendar page
# --------------------------------------------------------------------------- #

_NOTIONAL_HEADER_SYNONYMS = (
    "notified amount", "notional", "amount", "auction amount", "size",
)
_DATE_HEADER_SYNONYMS = ("auction date", "date", "issue date")
_TENOR_HEADER_SYNONYMS = ("tenor", "instrument", "security", "type", "tenure")


def parse_auction_calendar(
    html_text: str, *, today: date | None = None, horizon_weeks: int = CALENDAR_HORIZON_WEEKS,
) -> list[dict]:
    """Extract the forward per-tenor CALENDAR strip from the BB auction-calendar page.

    Pure (no I/O). Returns up to ``horizon_weeks`` worth of future weekly rows as
    ``{auction_date, tenor, notional?}`` — one per (date, tenor). Past-dated rows
    are dropped (the strip is forward-looking); a row missing a parseable date or
    a known tenor is skipped (partial-horizon handling — fewer rows is normal).
    Returns ``[]`` when no parseable calendar table is found (caller falls back to
    the LLM extract).
    """
    today = today or date.today()
    soup = BeautifulSoup(html_text, "html.parser")
    out: list[dict] = []

    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        header = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip().lower()
                  for c in trs[0].find_all(["td", "th"])]

        def _find_col(synonyms: tuple[str, ...], hdr: list[str] = header) -> int | None:
            for i, h in enumerate(hdr):
                if any(s in h for s in synonyms):
                    return i
            return None

        date_col = _find_col(_DATE_HEADER_SYNONYMS)
        notional_col = _find_col(_NOTIONAL_HEADER_SYNONYMS)
        tenor_col = _find_col(_TENOR_HEADER_SYNONYMS)
        if date_col is None:
            continue  # not a calendar table

        for tr in trs[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if date_col >= len(cells):
                continue
            row_date = _parse_date_token(cells[date_col])
            if row_date is None or row_date < today:
                continue  # un-parseable, or a past auction (forward strip only)
            # Tenor: prefer the dedicated column; else scan the whole row text.
            tenor = None
            if tenor_col is not None and tenor_col < len(cells):
                tenor = _tenor_label(cells[tenor_col])
            if tenor is None:
                tenor = _tenor_label(" ".join(cells))
            if tenor is None:
                continue
            row: dict = {"auction_date": row_date, "tenor": tenor}
            if notional_col is not None and notional_col < len(cells):
                notional = _in_range(_to_number(cells[notional_col]), _NOTIONAL_RANGE)
                if notional is not None:
                    row["notional"] = notional
            out.append(row)

    # Forward strip: chronological, capped at the horizon, deduped on (date, tenor).
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for row in sorted(out, key=lambda r: (r["auction_date"], r["tenor"])):
        key = (row["auction_date"].isoformat(), row["tenor"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    if horizon_weeks > 0:
        from datetime import timedelta

        horizon_end = today + timedelta(weeks=horizon_weeks)
        deduped = [r for r in deduped if r["auction_date"] <= horizon_end]
    return deduped


# --------------------------------------------------------------------------- #
# LLM fallback — multi-row strict-JSON extracts (used only when deterministic
# parse returns []). Mirrors the hybrid prompt contract (strict JSON), but the
# shape is a LIST of rows, so it lives outside the scalar hybrid path.
# --------------------------------------------------------------------------- #


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _flatten_text(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    return soup.get_text("\n", strip=True)


def _llm_rows(prompt_name: str, html_text: str, *, run_max_fn=run_max) -> list[dict]:
    """Call the Max LLM with a row-extract prompt; return the parsed row list.

    Returns ``[]`` on any failure (bad JSON, non-list, MaxCallError) so a flaky
    LLM never crashes the run — the deterministic parse already had first refusal.
    """
    template = _load_prompt(prompt_name)
    prompt = template.replace("{html_text}", _flatten_text(html_text)[:20000])
    try:
        result = run_max_fn(prompt=prompt)
    except MaxCallError as e:
        logger.warning("LLM row-extract (%s) failed: %s", prompt_name, e)
        return []
    parsed = result.parsed
    if parsed is None:
        try:
            parsed = json.loads(result.raw_text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("LLM row-extract (%s) returned non-JSON", prompt_name)
            return []
    rows = parsed.get("rows") if isinstance(parsed, dict) else parsed
    return rows if isinstance(rows, list) else []


def _coerce_result_rows(rows: list[dict], *, auction_date: date | None) -> list[dict]:
    """Normalise + range-gate LLM result rows into auction_results shape."""
    out: list[dict] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        tenor = _tenor_label(str(raw.get("tenor", "")))
        if tenor is None:
            continue
        row: dict = {"tenor": tenor}
        ad = raw.get("auction_date")
        parsed_ad = _parse_date_token(str(ad)) if ad else auction_date
        if parsed_ad is not None:
            row["auction_date"] = parsed_ad
        for field, bounds in _RESULT_FIELD_RANGE.items():
            val = _in_range(_to_number(str(raw.get(field, ""))), bounds)
            if val is not None:
                row[field] = val
        if "auction_date" in row and any(k in row for k in _RESULT_FIELD_RANGE):
            out.append(row)
    return out


def _coerce_calendar_rows(
    rows: list[dict], *, today: date, horizon_weeks: int,
) -> list[dict]:
    """Normalise + range-gate + forward-filter LLM calendar rows."""
    from datetime import timedelta

    horizon_end = today + timedelta(weeks=horizon_weeks)
    out: list[dict] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        tenor = _tenor_label(str(raw.get("tenor", "")))
        row_date = _parse_date_token(str(raw.get("auction_date", "")))
        if tenor is None or row_date is None:
            continue
        if row_date < today or row_date > horizon_end:
            continue
        row: dict = {"auction_date": row_date, "tenor": tenor}
        notional = _in_range(_to_number(str(raw.get("notional", ""))), _NOTIONAL_RANGE)
        if notional is not None:
            row["notional"] = notional
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def _get(url: str, *, session: requests.Session | None = None) -> str:
    sess = session or requests.Session()
    try:
        resp = sess.get(url, timeout=_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise FetchError(f"network error fetching {url}: {e}") from e
    if resp.status_code != 200:
        raise FetchError(f"{url} returned HTTP {resp.status_code}")
    return resp.text


def _get_rendered(url: str) -> str:
    """Fetch a BB page that sits behind the image-CAPTCHA wall, via the Playwright +
    claude-haiku solver in ``scrapers.bb_forex`` — the ONLY fetch path that actually
    clears BB's image-CAPTCHA (plain ``requests`` and html_fetcher's single JS-challenge
    reload do not). Lazy-imported so importing this module stays Playwright-free.

    R2: BB returns the image-CAPTCHA wall to the VPS datacenter IP for the press-release
    LISTING and the auction CALENDAR, so plain ``requests`` saw 0 ``/rrpt/`` anchors and
    raised a misleading "no anchors" error. Routing the listing/calendar through the
    solver fixes discovery; an unsolved wall now surfaces as a clear FetchError →
    needs_review. VPS-OPEN: confirm whether the per-release DETAIL page also walls (it is
    still fetched with plain ``requests`` below) and whether the calendar serves the same
    hard image-CAPTCHA vs only the lighter JS challenge."""
    from scrapers.bb_forex import fetch_rendered_html
    from scrapers.bb_forex_captcha import _is_captcha_page

    try:
        html = fetch_rendered_html(url)
    except Exception as e:  # solver exhausted (ParseError) / render failure
        raise FetchError(f"rendered fetch failed for {url}: {e}") from e
    if _is_captcha_page(html):
        raise FetchError(f"BB CAPTCHA unsolved for {url}")
    return html


def fetch_latest_results_html(*, session: requests.Session | None = None) -> str:
    """Discover (S7's rrpt logic) + GET the latest auction-result press release."""
    listing = _get_rendered(PRESS_RELEASE_LISTING_URL)
    try:
        target = discover_latest_rrpt_link(
            html=listing,
            base_url=PRESS_RELEASE_LISTING_URL,
            title_pattern=RESULTS_TITLE_PATTERN,
        )
    except ValueError as e:
        raise FetchError(f"results discovery failed: {e}") from e
    logger.info("discovered latest auction-result release: %s", target)
    return _get(target, session=session)


def fetch_calendar_html(*, session: requests.Session | None = None) -> str:
    """GET the BB forward auction-calendar page (behind the image-CAPTCHA wall).

    ``session`` is accepted for signature symmetry with fetch_latest_results_html but
    is IGNORED here: the calendar is fetched via the Playwright solver path, which
    cannot use a requests.Session."""
    return _get_rendered(AUCTION_CALENDAR_URL)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def scrape_results(
    *, session: requests.Session | None = None, run_max_fn=run_max,
) -> list[dict]:
    """Fetch + parse the latest results release into auction_results rows.

    Deterministic table parse first; LLM fallback only when it yields nothing.
    """
    html_text = fetch_latest_results_html(session=session)
    auction_date = recover_held_on(html_text)
    rows = parse_auction_results(html_text, auction_date=auction_date)
    if rows:
        return rows
    logger.info("results: deterministic parse empty — trying LLM fallback")
    llm = _llm_rows("auction_results_extract.txt", html_text, run_max_fn=run_max_fn)
    return _coerce_result_rows(llm, auction_date=auction_date)


def scrape_calendar(
    *, today: date | None = None, session: requests.Session | None = None,
    run_max_fn=run_max,
) -> list[dict]:
    """Fetch + parse the forward calendar into auction_calendar rows."""
    today = today or date.today()
    html_text = fetch_calendar_html(session=session)
    rows = parse_auction_calendar(html_text, today=today)
    if rows:
        return rows
    logger.info("calendar: deterministic parse empty — trying LLM fallback")
    llm = _llm_rows("auction_calendar_extract.txt", html_text, run_max_fn=run_max_fn)
    return _coerce_calendar_rows(llm, today=today, horizon_weeks=CALENDAR_HORIZON_WEEKS)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    today = datetime.now().date()
    exit_code = 0

    # RESULTS — independent of the calendar; a failure on one must not block the
    # other (intermittent sources). Each leg logs its own outcome.
    try:
        result_rows = scrape_results()
        logger.info("results: parsed %d per-tenor row(s)", len(result_rows))
        if result_rows:
            written = upsert_auction_results(result_rows)
            logger.info("results: upserted %d row(s) into auction_results", written)
        else:
            logger.info("results: no parseable rows (intermittent — no row written)")
    except (FetchError, SupabaseWriteError) as e:
        logger.exception("results leg failed")
        notify("error", "bb_auction results failed", str(e))
        exit_code = 1

    # CALENDAR
    try:
        calendar_rows = scrape_calendar(today=today)
        logger.info("calendar: parsed %d forward per-tenor row(s)", len(calendar_rows))
        if calendar_rows:
            written = upsert_auction_calendar(calendar_rows)
            logger.info("calendar: upserted %d row(s) into auction_calendar", written)
        else:
            logger.info("calendar: no parseable forward rows (partial/empty horizon)")
    except (FetchError, SupabaseWriteError) as e:
        logger.exception("calendar leg failed")
        notify("error", "bb_auction calendar failed", str(e))
        exit_code = 1

    return exit_code


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run

    sys.exit(wrap_run("bb_auction", "econdelta-auction.service", main))
