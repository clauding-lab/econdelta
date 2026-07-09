"""One-time backfill: DSE per-share daily-close history for the DS30 blue-chips.

Source
------
DSE Day End Archive — one HTTP GET per scrip returns the full requested date
range as an HTML table:

    https://www.dsebd.org/day_end_archive.php
        ?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&inst=<CODE>&archive=data

The data table header (verified 2026-05-30) is:

    # | DATE | TRADING CODE | LTP* | HIGH | LOW | OPENP* | CLOSEP* | YCP
      | TRADE | VALUE (mn) | VOLUME

We keep the closing price (``CLOSEP*``) per trading day and write one
``metric_history`` row per (scrip, date):

    metric_id = "dse_close_<CODE>"   e.g. dse_close_BRACBANK
    as_of     = the trading DATE     (per-row, not the run date)
    value     = CLOSEP*              (numeric, taka)
    source    = "DSE Day End Archive"

The DS30 constituent list (30 trading codes) comes from
``https://dsebd.org/dse30_share.php`` — the single 30-row table on the page.

This mirrors econdelta conventions:
  * ``utils.http_client.HttpClient`` for the polite retrying session.
  * ``utils.supabase_writer.upsert_metric_history`` for the write, which
    accepts a per-metric ``source_as_of_map`` so each close lands under its
    own trading date.
  * ``utils.calendar`` only loosely — DSE already returns trading days, so we
    trust the source's DATE column rather than reconstructing the calendar.

ONE-TIME backfill. Not wired into the daily timers. Returns are derived later
inside The Brief — this script stores RAW closes only.

Usage
-----
Dry run (fetch + parse + print, writes NOTHING):

    PYTHONPATH=/path/to/econdelta \\
        /path/to/econdelta/.venv/bin/python backfill_dse_dayend.py --dry-run

A dry run with no extra flags samples the DS30 list plus the first 3 scrips
over the last 60 days and prints parsed rows. To dry-run the full set over a
custom window:

    backfill_dse_dayend.py --dry-run --start 2026-03-01 --end 2026-05-30 --all

Real backfill (writes to Supabase — requires SUPABASE_URL + service key):

    backfill_dse_dayend.py --start 2026-03-01 --end 2026-05-30
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta

from bs4 import BeautifulSoup

from utils.http_client import HttpClient
from utils.notifier import notify

logger = logging.getLogger("backfill_dse_dayend")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DS30_URL = "https://dsebd.org/dse30_share.php"
ARCHIVE_URL = "https://www.dsebd.org/day_end_archive.php"
SOURCE_LABEL = "DSE Day End Archive"
METRIC_PREFIX = "dse_close_"

# dsebd.org occasionally rejects the default econdelta UA on these PHP pages;
# a browser UA is reliable from BD egress. Kept here, not in http_client, so we
# don't perturb the shared client other scrapers depend on.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Politeness: sequential requests with a small delay between scrips.
_REQUEST_DELAY_S = 2.0
_DEFAULT_LOOKBACK_DAYS = 60
_DRY_RUN_SAMPLE_SCRIPS = 3

# Alert floor for the DAILY production path (dse_dayend). DS30 is 30 tickers; a
# full run that lands fewer than this many distinct tickers means the source or
# TLS chain is degrading (the 24-silent-day DSE freeze was exactly this, unwatched).
# Only enforced when notify_on_failure=True AND the run covers the full DS30 set.
_TICKER_FLOOR = 25

# Sanity envelope for a parsed close (taka). DS30 names trade well within this.
_MIN_CLOSE = 0.0
_MAX_CLOSE = 100_000.0


class BackfillError(Exception):
    """Raised on an unrecoverable parse/shape problem."""


@dataclass(frozen=True)
class CloseRow:
    """One parsed (scrip, trading-day, close) observation."""

    code: str
    as_of: date
    closep: float

    @property
    def metric_id(self) -> str:
        return f"{METRIC_PREFIX}{self.code}"


# --------------------------------------------------------------------------- #
# Pure parse functions (no I/O — directly unit-testable)
# --------------------------------------------------------------------------- #


def _clean_number(text: str) -> float:
    """Strip thousands separators / stray chars and parse a float."""
    cleaned = text.strip().replace(",", "").rstrip("%")
    return float(cleaned)


def _find_data_table(soup: BeautifulSoup, *, required_headers: list[str]):
    """Return the first <table> whose header row contains all required headers.

    DSE wraps the real grid in many sibling tables; we identify the data table
    by its header text rather than a brittle positional index.
    """
    wanted = [h.upper() for h in required_headers]
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_text = rows[0].get_text(" ", strip=True).upper()
        if all(w in header_text for w in wanted):
            return table
    return None


def _header_index(header_cells: list[str], *candidates: str) -> int:
    """Return the column index whose stripped/upper header matches a candidate.

    Matching is tolerant of trailing markers like the '*' DSE appends to
    CLOSEP / LTP / OPENP / YCP column labels.
    """
    norm = [c.strip().upper().rstrip("*").strip() for c in header_cells]
    for cand in candidates:
        target = cand.strip().upper().rstrip("*").strip()
        for i, h in enumerate(norm):
            if h == target:
                return i
    raise BackfillError(
        f"none of columns {candidates!r} found in header {header_cells!r}"
    )


def parse_ds30_codes(html: str) -> list[str]:
    """Extract the 30 DS30 trading codes from dse30_share.php.

    The page lists every DSE scrip in a dropdown plus one dedicated 30-row
    table for the DS30 index members. We pick the table whose header carries
    'TRADING CODE' and 'CLOSEP' and has exactly 30 ``displayCompany`` links.
    """
    soup = BeautifulSoup(html, "html.parser")

    best: list[str] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_text = rows[0].get_text(" ", strip=True).upper()
        if "TRADING CODE" not in header_text or "CLOSEP" not in header_text:
            continue
        codes: list[str] = []
        for a in table.find_all("a", href=True):
            m = re.search(r"displayCompany\.php\?name=([A-Z0-9]+)", a["href"])
            if m:
                code = m.group(1)
                if code not in codes:
                    codes.append(code)
        # The DS30 members table holds exactly 30 scrips; prefer it. Fall back
        # to the largest matching table so a small layout drift still parses.
        if len(codes) == 30:
            return codes
        if len(codes) > len(best):
            best = codes

    if not best:
        raise BackfillError("DS30 page: no table with trading-code links found")
    logger.warning(
        "DS30 page: no exact 30-row table; using best match with %d codes",
        len(best),
    )
    return best


def parse_day_end_archive(html: str, *, expected_code: str | None = None) -> list[CloseRow]:
    """Parse the Day End Archive grid into ``CloseRow`` records.

    Args:
        html: Raw HTML of one day_end_archive.php?...&archive=data response.
        expected_code: If given, rows whose TRADING CODE differs are skipped
            (defensive — the endpoint already filters by ``inst``).

    Returns:
        One ``CloseRow`` per trading-day row, sorted ascending by date.

    Raises:
        BackfillError: If the data table or its DATE/CLOSEP columns are missing.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = _find_data_table(
        soup, required_headers=["DATE", "TRADING CODE", "CLOSEP"]
    )
    if table is None:
        raise BackfillError("day-end archive: no DATE/CLOSEP data table found")

    rows = table.find_all("tr")
    header_cells = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    date_idx = _header_index(header_cells, "DATE")
    code_idx = _header_index(header_cells, "TRADING CODE")
    close_idx = _header_index(header_cells, "CLOSEP")

    out: list[CloseRow] = []
    for tr in rows[1:]:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        # Need enough columns and a date-shaped first data field.
        if len(cells) <= max(date_idx, code_idx, close_idx):
            continue
        raw_date = cells[date_idx].strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            continue  # skip totals / spacer / non-data rows
        code = cells[code_idx].strip().upper()
        if expected_code and code != expected_code.upper():
            continue
        try:
            as_of = date.fromisoformat(raw_date)
            closep = _clean_number(cells[close_idx])
        except (ValueError, TypeError):
            logger.warning("skipping unparseable row for %s: %r", code, cells)
            continue
        if not (_MIN_CLOSE < closep <= _MAX_CLOSE):
            logger.warning(
                "skipping out-of-range close for %s @ %s: %s", code, raw_date, closep
            )
            continue
        out.append(CloseRow(code=code, as_of=as_of, closep=closep))

    out.sort(key=lambda r: r.as_of)
    return out


def rows_to_supabase_payload(
    rows: list[CloseRow],
) -> tuple[dict[str, float], dict[str, date]]:
    """Translate ``CloseRow`` records into upsert_metric_history inputs.

    ``upsert_metric_history`` takes a flat ``data`` dict {metric_id: value} plus
    a ``source_as_of_map`` {metric_id: date}. Because each (scrip, day) pair is
    its own metric_id+as_of row, we cannot fold a multi-day series into one
    flat dict in a single call. The backfill therefore upserts ONE call per
    trading day (see ``run_backfill``). This helper builds the per-day slice.

    Given rows that all share the SAME as_of, returns:
        data            = {metric_id: closep}
        source_as_of_map = {metric_id: as_of}
    """
    data: dict[str, float] = {}
    as_of_map: dict[str, date] = {}
    for r in rows:
        data[r.metric_id] = r.closep
        as_of_map[r.metric_id] = r.as_of
    return data, as_of_map


def group_rows_by_date(rows: list[CloseRow]) -> dict[date, list[CloseRow]]:
    """Group flat CloseRows into {trading_day: [rows]} for per-day upserts."""
    grouped: dict[date, list[CloseRow]] = {}
    for r in rows:
        grouped.setdefault(r.as_of, []).append(r)
    return grouped


# --------------------------------------------------------------------------- #
# Fetch helpers (I/O)
# --------------------------------------------------------------------------- #


def _make_client() -> HttpClient:
    client = HttpClient()
    client._session.headers.update({"User-Agent": _BROWSER_UA})  # noqa: SLF001
    return client


def fetch_ds30_codes(client: HttpClient) -> list[str]:
    html = client.fetch_html(DS30_URL)
    return parse_ds30_codes(html)


def fetch_scrip_closes(
    client: HttpClient, code: str, start: date, end: date
) -> list[CloseRow]:
    params = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "inst": code,
        "archive": "data",
    }
    html = client.fetch_html(ARCHIVE_URL, params=params)
    return parse_day_end_archive(html, expected_code=code)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _print_sample(code: str, rows: list[CloseRow], limit: int = 5) -> None:
    print(f"\n  [{code}] {len(rows)} trading-day close(s) parsed")
    head = rows[:limit]
    tail = rows[-limit:] if len(rows) > limit else []
    for r in head:
        print(f"    {{'code': '{r.code}', 'date': '{r.as_of.isoformat()}', 'closep': {r.closep}}}  -> metric_id={r.metric_id}")
    if tail and tail != head:
        print("    ...")
        for r in tail:
            print(f"    {{'code': '{r.code}', 'date': '{r.as_of.isoformat()}', 'closep': {r.closep}}}  -> metric_id={r.metric_id}")


def run_backfill(
    *,
    start: date,
    end: date,
    dry_run: bool,
    sample_only: bool,
    codes_override: list[str] | None = None,
    notify_on_failure: bool = False,
) -> int:
    """Fetch DS30 closes and either print (dry-run) or upsert (real).

    ``notify_on_failure`` (default False so manual backfills and dry-runs stay
    quiet) is set True by the daily ``scrapers.dse_dayend`` production path so a
    total fetch failure, a below-floor partial, or a Supabase write error fires a
    Discord ``error`` alert instead of failing silently — this scraper was the
    ONLY one without alerting, which is how the DSE feed froze unnoticed for 24
    days (E1.6).
    """
    client = _make_client()

    if codes_override:
        codes = codes_override
        print(f"Using {len(codes)} override code(s): {codes}")
    else:
        print(f"Fetching DS30 constituent list from {DS30_URL} ...")
        codes = fetch_ds30_codes(client)
        print(f"DS30 list: {len(codes)} codes -> {codes}")

    if sample_only:
        codes = codes[:_DRY_RUN_SAMPLE_SCRIPS]
        print(
            f"\n--dry-run sample mode: fetching first {len(codes)} scrip(s) "
            f"over {start.isoformat()} .. {end.isoformat()}"
        )

    all_rows: list[CloseRow] = []
    for i, code in enumerate(codes):
        if i:
            time.sleep(_REQUEST_DELAY_S)  # be polite to dsebd.org
        try:
            rows = fetch_scrip_closes(client, code, start, end)
        except (HttpClient.FetchError, BackfillError) as e:
            logger.error("fetch/parse failed for %s: %s", code, e)
            print(f"  [{code}] FAILED: {type(e).__name__}: {e}")
            continue
        all_rows.extend(rows)
        if dry_run:
            _print_sample(code, rows)

    print(
        f"\nTotal parsed: {len(all_rows)} close rows across {len(codes)} scrip(s) "
        f"-> {len({r.metric_id for r in all_rows})} distinct metric_ids."
    )

    if dry_run:
        print("\nDRY RUN — nothing written to Supabase.")
        return 0 if all_rows else 1

    # --- Real write path ----------------------------------------------------
    from utils.supabase_writer import SupabaseWriteError, upsert_metric_history

    if not all_rows:
        print("No rows parsed; nothing to upsert.")
        if notify_on_failure:
            notify(
                "error",
                "dse_dayend — zero tickers written",
                f"No DS30 closes parsed for window {start.isoformat()}..{end.isoformat()}; "
                f"all {len(codes)} ticker fetches failed (likely a DSE TLS-chain break "
                "or host block).",
            )
        return 1

    distinct = len({r.metric_id for r in all_rows})
    is_full_run = codes_override is None and not sample_only
    if notify_on_failure and is_full_run and distinct < _TICKER_FLOOR:
        notify(
            "error",
            "dse_dayend — below ticker floor",
            f"Only {distinct}/{len(codes)} DS30 tickers parsed for window "
            f"{start.isoformat()}..{end.isoformat()} (floor {_TICKER_FLOOR}); the source "
            "may be partially degraded. Writing the partial set.",
        )

    total = 0
    try:
        for trading_day, day_rows in sorted(group_rows_by_date(all_rows).items()):
            data, as_of_map = rows_to_supabase_payload(day_rows)
            n = upsert_metric_history(
                data=data,
                as_of=trading_day,
                source=SOURCE_LABEL,
                source_as_of_map=as_of_map,
            )
            total += n
            logger.info("upserted %d rows for %s", n, trading_day.isoformat())
    except SupabaseWriteError as e:
        if notify_on_failure:
            notify(
                "error",
                "dse_dayend — Supabase write failed",
                f"metric_history upsert failed after {total} row(s) for window "
                f"{start.isoformat()}..{end.isoformat()}: {type(e).__name__}: {e}",
            )
        raise
    print(f"Upserted {total} rows to metric_history.")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DSE DS30 day-end close backfill")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch + parse + print sample rows; write NOTHING.")
    p.add_argument("--start", type=str, default=None,
                   help="Start date YYYY-MM-DD (default: today-60d).")
    p.add_argument("--end", type=str, default=None,
                   help="End date YYYY-MM-DD (default: today).")
    p.add_argument("--all", action="store_true",
                   help="Process all 30 scrips (default in --dry-run is a 3-scrip sample).")
    p.add_argument("--codes", type=str, default=None,
                   help="Comma-separated override codes (skips DS30 list fetch).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = (
        date.fromisoformat(args.start)
        if args.start
        else end - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
    )
    if start > end:
        print("ERROR: --start must be on or before --end", file=sys.stderr)
        return 2

    codes_override = (
        [c.strip().upper() for c in args.codes.split(",") if c.strip()]
        if args.codes
        else None
    )

    # In --dry-run, default to a 3-scrip sample unless --all/--codes is given.
    sample_only = args.dry_run and not args.all and codes_override is None

    return run_backfill(
        start=start,
        end=end,
        dry_run=args.dry_run,
        sample_only=sample_only,
        codes_override=codes_override,
    )


if __name__ == "__main__":
    sys.exit(main())
