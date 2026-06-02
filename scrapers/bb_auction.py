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

TWO sources, ONE scraper (both repointed after BB's 2026-06 restructure — the old
per-business-day ``/rrpt/`` press release became a PDF behind an F5 + image-CAPTCHA
wall the renderer cannot clear; see AGENT_LEARNINGS / AGENTS landmine 24):

  RESULTS (``auction_results``) — per-tenor results for auctions that HAVE happened,
    read from the ``monetaryactivity/treasury`` HTML auction-results table (the same
    solver-served page that lands the scalar cut-off yields ``bill_bond_rates`` /
    ``tbill_*_yield``). Per tenor: accepted SIZE, total BID, derived BID-COVER, the
    weighted-average MATURITY (WAM, bonds, from the re-issuance note), and the CUTOFF
    yield. ``parse_treasury_results``.

  CALENDAR (``auction_calendar``) — the forward ISSUANCE strip, read from the
    ``monetaryactivity/auc_calendar/1`` ("Yearly calendar") div-grid (bills + bonds).
    Emits ALL future per-tenor ``{auction_date, tenor, notional}`` rows within the
    horizon. ``parse_yearly_calendar``. (The scalar ``gsec_auction`` still points at
    the bare ``auc_calendar`` separately.)

BD EGRESS (CAPTCHA wall confirmed): BB firewalls non-BD IPs, so the live fetch +
parse of BOTH sources is VPS-deferred (ExonVPS Dhaka, where the cron runs). The
parse helpers are PURE (operate on captured HTML text) so they unit-test fully
offline against REAL box-capture fixtures; the live fetch is what the VPS run
confirms (page shape, row labels, column order, date format).

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

from bs4 import BeautifulSoup

from claude_max.max_client import MaxCallError, run_max
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

# RESULTS (primary, post-restructure): the monetaryactivity/treasury auction-results
# table — the SAME solver-served HTML page that already lands the scalar cut-off
# yields (bill_bond_rates / tbill_*_yield). Replaces the retired /rrpt/ press release,
# which BB turned into a PDF behind an F5 + image-CAPTCHA wall that does not yield to
# the renderer (see AGENT_LEARNINGS — R2 auction restructure).
AUCTION_RESULTS_URL = "https://www.bb.org.bd/en/index.php/monetaryactivity/treasury"

# CALENDAR: the YEARLY auction calendar (auc_calendar/1) — the forward issuance strip
# as a div-grid. NOTE: the bare auc_calendar ("Yet to bid") page no longer renders a
# server-side table; the scalar gsec_auction still points at it separately.
AUCTION_CALENDAR_URL = "https://www.bb.org.bd/en/index.php/monetaryactivity/auc_calendar/1"

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


# Per-field validation ranges, shared by the deterministic treasury parse and the
# LLM-fallback coerce (``_coerce_result_rows``).
_RESULT_FIELD_RANGE = {
    "size": _SIZE_RANGE, "bid": _BID_RANGE, "cover": _COVER_RANGE,
    "wam": _WAM_RANGE, "cutoff": _CUTOFF_RANGE,
}


# --------------------------------------------------------------------------- #
# RESULTS (primary source) — per-tenor rows from the monetaryactivity/treasury
# auction-results HTML table. BB retired the per-business-day /rrpt/ press release
# (now a PDF behind an F5 + image-CAPTCHA wall that does NOT yield to the renderer),
# so RESULTS come from this already-solver-served page. The table has a TWO-ROW
# grouped header — "Bids received" (colspan 3) + "Bids accepted" (colspan 7) — so
# "Face value"/"Range of yields" appear twice; columns are mapped by header GROUP,
# not position (landmine E).
# --------------------------------------------------------------------------- #

# WAM of a re-issued bond is printed in the ISIN cell, e.g.
# "BD0931401204 (Re-issuance: 4.96 Yr.)"; plain bills carry no such note.
_REISSUANCE_RE = re.compile(r"re-?issuance:\s*([\d.]+)\s*yr", re.IGNORECASE)
_TREASURY_REQUIRED = ("auction_date", "tenor", "bid", "size", "cutoff")


def _header_rowcount(trs: list) -> int:
    """1 if the first header row has no colspan groups, else 2 (grouped header)."""
    if not trs:
        return 0
    first = trs[0].find_all(["th", "td"])
    return 2 if any(int(c.get("colspan") or 1) > 1 for c in first) else 1


def _expand_header(trs: list) -> list[tuple[str, str]]:
    """Flatten a 1- or 2-row table header into one (group, sub) label per column.

    A standalone column (rowspan=2, or no colspan) carries its label as the group
    with an empty sub. A group header (colspan=N) spans N columns whose sub-labels
    come from the second header row. Returns [] when there is no header row.
    """
    if not trs:
        return []
    row0 = trs[0].find_all(["th", "td"])
    if not row0:
        return []
    if _header_rowcount(trs) == 1:
        return [(c.get_text(" ", strip=True), "") for c in row0]
    subs = trs[1].find_all(["th", "td"]) if len(trs) > 1 else []
    sub_iter = iter(c.get_text(" ", strip=True) for c in subs)
    flat: list[tuple[str, str]] = []
    for c in row0:
        label = c.get_text(" ", strip=True)
        colspan = int(c.get("colspan") or 1)
        rowspan = int(c.get("rowspan") or 1)
        if rowspan >= 2 or colspan == 1:
            flat.append((label, ""))
        else:
            for _ in range(colspan):
                flat.append((label, next(sub_iter, "")))
    return flat


def _treasury_field_map(flat: list[tuple[str, str]]) -> dict[str, int]:
    """Map canonical field -> flat column index by header LABEL (group-aware)."""
    fmap: dict[str, int] = {}
    for idx, (group, sub) in enumerate(flat):
        g, s = group.lower(), sub.lower()
        if "auction_date" not in fmap and "issue date" in g:
            fmap["auction_date"] = idx
        if "tenor" not in fmap and "remaining maturity" in g:
            fmap["tenor"] = idx
        if "isin" not in fmap and "isin" in g:
            fmap["isin"] = idx
        if "bid" not in fmap and "received" in g and "face value" in s:
            fmap["bid"] = idx
        if "size" not in fmap and "accepted" in g and "face value" in s:
            fmap["size"] = idx
        if "cutoff" not in fmap and "cut" in s and "off" in s:
            fmap["cutoff"] = idx
    return fmap


def parse_treasury_results(html_text: str) -> list[dict]:
    """Extract per-tenor RESULTS rows from the BB monetaryactivity/treasury table.

    Pure (no I/O). Returns ``{auction_date, tenor, size?, bid?, cover?, wam?,
    cutoff?}`` — one row per canonical tenor present (non-canonical tenors such as
    the 14-day bill or 3-year FRTB are dropped by ``_tenor_label``). ``cover`` is
    derived (bid/size, 2dp); ``wam`` is read from a bond's re-issuance note. Returns
    [] when no results table is found, so the caller falls through to the LLM extract.

    NOTE: ``auction_date`` is the table's *Issue date* (settlement) — ~1 business
    day after the auction was held. The page exposes no held-on date; (auction_date,
    tenor) stays a stable, unique PK, and this is the only structured HTML source
    not behind the binary PDF wall.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        flat = _expand_header(trs)
        if not flat:
            continue
        fmap = _treasury_field_map(flat)
        if not all(k in fmap for k in _TREASURY_REQUIRED):
            continue  # not the results table (e.g. the yield-curve summary table)
        rows: list[dict] = []
        for tr in trs[_header_rowcount(trs):]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < len(flat):
                continue
            tenor = _tenor_label(cells[fmap["tenor"]])
            auction_date = _parse_date_token(cells[fmap["auction_date"]])
            if tenor is None or auction_date is None:
                continue
            size = _in_range(_to_number(cells[fmap["size"]]), _SIZE_RANGE)
            bid = _in_range(_to_number(cells[fmap["bid"]]), _BID_RANGE)
            cutoff = _in_range(_to_number(cells[fmap["cutoff"]]), _CUTOFF_RANGE)
            row: dict = {"auction_date": auction_date, "tenor": tenor}
            if size is not None:
                row["size"] = size
            if bid is not None:
                row["bid"] = bid
            if cutoff is not None:
                row["cutoff"] = cutoff
            if size and bid is not None:
                cover = _in_range(round(bid / size, 2), _COVER_RANGE)
                if cover is not None:
                    row["cover"] = cover
            isin_idx = fmap.get("isin")
            if isin_idx is not None and isin_idx < len(cells):
                m = _REISSUANCE_RE.search(cells[isin_idx])
                if m:
                    wam = _in_range(_to_number(m.group(1)), _WAM_RANGE)
                    if wam is not None:
                        row["wam"] = wam
            if any(k in row for k in ("size", "bid", "cover", "wam", "cutoff")):
                rows.append(row)
        return rows
    return []


# --------------------------------------------------------------------------- #
# CALENDAR — forward per-tenor issuance strip from the YEARLY auction calendar.
# BB also restructured the calendar: the old "Yet to bid" page (auc_calendar) no
# longer renders a server-side <table>, and the forward strip moved to
# auc_calendar/1 ("Yearly calendar") — a CSS DIV-GRID: div.row-header + div.row-data
# with div.column cells, TWO grids in document order (BILLS: 14/91/182/364 days;
# BONDS: 2/5/10/15/20 yr + 3 yr FRTB), each preceded by its own header. Columns map
# by the CURRENT grid's header (landmine E); a per-tenor cell is the notified amount
# (0.00 == no auction of that tenor that date).
# --------------------------------------------------------------------------- #


def _calendar_columns(div) -> list[str]:
    return [c.get_text(" ", strip=True) for c in div.find_all("div", class_="column")]


def parse_yearly_calendar(
    html_text: str, *, today: date | None = None, horizon_weeks: int = CALENDAR_HORIZON_WEEKS,
) -> list[dict]:
    """Extract the forward CALENDAR strip from the BB yearly-calendar div-grid.

    Pure (no I/O). Walks the grid in document order: each ``div.row-header`` sets the
    column->canonical-tenor map (and the date column) for the ``div.row-data`` rows
    that follow, so the BILLS and BONDS grids each map by their OWN header. Emits
    ``{auction_date, tenor, notional}`` for every FUTURE (>= today) row × canonical
    tenor with a non-zero notified amount; non-canonical tenors (14-day, 3 yr FRTB)
    and zero cells are skipped. Deduped on (date, tenor), capped at ``horizon_weeks``.
    Returns [] when no grid is found (caller falls through to the LLM extract).
    """
    today = today or date.today()
    soup = BeautifulSoup(html_text, "html.parser")
    out: list[dict] = []
    tenor_cols: dict[int, str] = {}
    date_idx = 1
    for div in soup.find_all("div"):
        cls = div.get("class") or []
        if "row-header" in cls:
            labels = _calendar_columns(div)
            tenor_cols = {
                i: t for i, lbl in enumerate(labels) if (t := _tenor_label(lbl)) is not None
            }
            date_idx = next((i for i, lbl in enumerate(labels) if "date" in lbl.lower()), 1)
        elif "row-data" in cls and tenor_cols:
            cells = _calendar_columns(div)
            if date_idx >= len(cells):
                continue
            row_date = _parse_date_token(cells[date_idx])
            if row_date is None or row_date < today:
                continue  # un-parseable, or a past auction (forward strip only)
            for idx, tenor in tenor_cols.items():
                if idx >= len(cells):
                    continue
                notional = _in_range(_to_number(cells[idx]), _NOTIONAL_RANGE)
                if notional and notional > 0:  # 0.00 == no auction of this tenor
                    out.append({"auction_date": row_date, "tenor": tenor, "notional": notional})

    # Forward strip: chronological, deduped on (date, tenor), capped at the horizon.
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


def _get_rendered(url: str) -> str:
    """Fetch a BB page that sits behind the image-CAPTCHA wall, via the Playwright +
    claude-haiku solver in ``scrapers.bb_forex`` — the ONLY fetch path that actually
    clears BB's image-CAPTCHA (plain ``requests`` and html_fetcher's single JS-challenge
    reload do not). Lazy-imported so importing this module stays Playwright-free.

    R2: BB walls both the auction RESULTS page (monetaryactivity/treasury) and the
    auction CALENDAR (monetaryactivity/auc_calendar) to the VPS datacenter IP, so both
    are routed through the solver. An unsolved wall surfaces as a clear FetchError →
    needs_review (not a misleading downstream parse failure)."""
    from scrapers.bb_forex import fetch_rendered_html
    from scrapers.bb_forex_captcha import _is_captcha_page

    try:
        html = fetch_rendered_html(url)
    except Exception as e:  # solver exhausted (ParseError) / render failure
        raise FetchError(f"rendered fetch failed for {url}: {e}") from e
    if _is_captcha_page(html):
        raise FetchError(f"BB CAPTCHA unsolved for {url}")
    return html


def fetch_results_html() -> str:
    """GET the BB monetaryactivity/treasury auction-results page (behind the wall).

    Post-restructure source: BB retired the per-business-day /rrpt/ auction-result
    press release (now a PDF behind an F5 + image-CAPTCHA wall the renderer cannot
    clear), so per-tenor RESULTS come from this solver-served HTML table — the SAME
    page that already lands the scalar cut-off yields (bill_bond_rates / tbill_*)."""
    return _get_rendered(AUCTION_RESULTS_URL)


def fetch_calendar_html() -> str:
    """GET the BB yearly auction-calendar div-grid (behind the image-CAPTCHA wall),
    via the Playwright solver path."""
    return _get_rendered(AUCTION_CALENDAR_URL)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def scrape_results(*, run_max_fn=run_max) -> list[dict]:
    """Fetch + parse the treasury auction-results table into auction_results rows.

    Deterministic table parse first; LLM fallback only when it yields nothing. The
    LLM fallback carries no shared auction_date (the treasury table is per-row dated),
    so coerce relies on each row's own date and drops any row lacking one.
    """
    html_text = fetch_results_html()
    rows = parse_treasury_results(html_text)
    if rows:
        return rows
    logger.info("results: deterministic parse empty — trying LLM fallback")
    llm = _llm_rows("auction_results_extract.txt", html_text, run_max_fn=run_max_fn)
    return _coerce_result_rows(llm, auction_date=None)


def scrape_calendar(
    *, today: date | None = None, run_max_fn=run_max,
) -> list[dict]:
    """Fetch + parse the forward calendar into auction_calendar rows."""
    today = today or date.today()
    html_text = fetch_calendar_html()
    rows = parse_yearly_calendar(html_text, today=today)
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
