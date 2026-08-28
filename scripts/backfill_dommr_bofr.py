"""One-time backfill: DOMMR/BOFR reference-rate history from BB's Money
Market Reference Rate page.

Source
------
https://www.bb.org.bd/en/index.php/monetaryactivity/money_market_ref_rate

The default GET shows only the latest business day. History comes from a
POST to the SAME URL with form field ``date_picker`` set to
``dd/mm/yyyy - dd/mm/yyyy`` (space-hyphen-space) — the page then renders ALL
business days in the range, newest first, as extra date-header blocks inside
the same two header-anchored tables (DOMMR: Overnight/1W/1M/3M; BOFR:
Overnight/1W). The page sits behind BB's F5/TSPD JS challenge, so the fetch
is Playwright-driven with the SAME stealth machinery ``fetchers/
html_fetcher.py`` uses: fill ``#date_picker``, submit ``#search-form``, read
the rendered document.

Parsing REUSES the anchored logic in ``parsers/html_money_market_ref_rate``
(``find_rate_table`` / ``extract_date_blocks`` / date-header parsing), so the
backfill and the daily pipeline can never disagree on table anchoring
(AGENTS.md landmine 45: anchor by header text, never table order).

Hard filters (applied in ``blocks_to_rows``, unit-tested against the real
fixture):

- ``value_date >= 2026-04-15`` — the series' genuine launch. Rows BEFORE
  15 Apr 2026 are BB's pre-launch STAGING TEST DATA (tenor label ``7D``
  where production uses ``1W``, repeated identical tuples, nonsense rows)
  and must be excluded everywhere.
- ``tenor label != '7D'`` — the same staging tell, filtered per-row as
  belt-and-suspenders on top of the date cut.

Rows written per business day (5 ids): the four fanned series ids
``dommr`` / ``dommr_1w`` / ``bofr`` / ``bofr_1w`` PLUS the parent headline
``money_market_ref_rate`` (= the DOMMR Overnight value) — exactly the ids
the daily fan-out in ``aggregate_latest._flatten_dict_indicators`` writes
going forward, so the parent's history is continuous from launch instead of
starting at deploy date. Every row's ``as_of`` is the page's OWN date-header
date via a per-row ``source_as_of_map`` — never the run date (landmine 47).

ONE-TIME backfill. NOT wired to any timer. Mirrors
``scripts/backfill_dse_dayend.py``: ``upsert_metric_history`` with per-date
batches (landmine 22: NEVER pass ``url=`` — that is the Supabase base-URL
override, not provenance), ``verify_landed_count`` read-back after the write.

Expect roughly ~90 business days for 15 Apr → late Aug 2026 — BD weekends
(Fri/Sat) and public holidays are simply ABSENT from the source (e.g.
2026-08-26 Mawlid), so never assert calendar-day counts.

Usage (run from the repo root so ``parsers``/``utils`` resolve)
-----
Dry run (fetch + parse + print row counts and a sample, writes NOTHING):

    .venv/bin/python -m scripts.backfill_dommr_bofr --dry-run

Real backfill (writes to Supabase — requires SUPABASE_URL + service key):

    .venv/bin/python -m scripts.backfill_dommr_bofr --start 2026-04-15 --end 2026-08-28

Be gentle: one POST per chunk (default ~50-day chunks), a few seconds
between. The site returned a 4-month range in one response without
pagination, but a rate-limited host earns small requests anyway.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from parsers.html_money_market_ref_rate import (
    BOFR_HEADER,
    DOMMR_HEADER,
    extract_date_blocks,
    find_rate_table,
)

logger = logging.getLogger("backfill_dommr_bofr")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PAGE_URL = "https://www.bb.org.bd/en/index.php/monetaryactivity/money_market_ref_rate"
SOURCE_LABEL = "BB Money Market Reference Rate"

# Genuine series launch. Anything earlier in a range response is staging
# test data and is hard-filtered.
LAUNCH_DATE = date(2026, 4, 15)

# Staging-test-data tenor label (production uses "1W").
_TEST_DATA_TENOR = "7d"

# (header anchor, series prefix) per table. The parser module owns the
# header strings; prefixes here mint the same ids the daily fan-out mints
# (aggregate_latest.MONEY_MARKET_REF_RATE_FANOUT_IDS).
_TABLE_SPECS: tuple[tuple[str, str], ...] = (
    (DOMMR_HEADER, "dommr"),
    (BOFR_HEADER, "bofr"),
)
_TENOR_SUFFIX: dict[str, str] = {"overnight": "", "1w": "_1w"}

# Parent headline id — mirrors _flatten_dict_indicators' promotion of the
# DOMMR Overnight rate to the indicator's own scalar.
PARENT_METRIC_ID = "money_market_ref_rate"

# Politeness: one POST per chunk, pause between chunks.
_CHUNK_DAYS_DEFAULT = 50
_CHUNK_DELAY_S = 5.0

# Same stealth surface fetchers/html_fetcher.py uses.
_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
_GOTO_TIMEOUT_MS = 90_000
_CHALLENGE_SETTLE_MS = 10_000
_RESULT_SETTLE_MS = 8_000
_CHALLENGE_MARKERS: tuple[str, ...] = ("Pardon Our Interruption", "support ID is:")

# Sanity envelope for a parsed rate (%) — mirrors the config valid_range.
_MIN_RATE = 0.0
_MAX_RATE = 25.0


class BackfillError(Exception):
    """Raised on an unrecoverable parse/shape problem."""


@dataclass(frozen=True)
class RateRow:
    """One parsed (metric_id, business-day, rate) observation."""

    metric_id: str
    as_of: date
    value: float


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O — directly unit-testable)
# --------------------------------------------------------------------------- #


def format_date_picker_range(start: date, end: date) -> str:
    """BB's expected form value: ``dd/mm/yyyy - dd/mm/yyyy`` (space-hyphen-space)."""
    return f"{start.strftime('%d/%m/%Y')} - {end.strftime('%d/%m/%Y')}"


def chunk_ranges(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    """Split [start, end] into inclusive chunks of at most ``chunk_days`` days."""
    if start > end:
        raise ValueError(f"start {start} after end {end}")
    if chunk_days < 1:
        raise ValueError("chunk_days must be >= 1")
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        out.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return out


def parse_history_html(html: str) -> list[RateRow]:
    """Parse ONE range-response document into per-row observations.

    Uses the SAME header-anchored table selection + date-block walk as the
    daily parser, over ALL date blocks (not just the newest). Applies the
    hard filters: ``value_date >= LAUNCH_DATE`` and ``tenor != '7D'``.
    Out-of-range rates are skipped with a warning (never written). The
    parent headline row is minted per date from the DOMMR Overnight value.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[RateRow] = []
    dommr_overnight_by_date: dict[date, float] = {}

    for header_text, prefix in _TABLE_SPECS:
        table = find_rate_table(soup, header_text)
        for value_date, tenors in extract_date_blocks(table):
            if value_date < LAUNCH_DATE:
                logger.info(
                    "%s %s: pre-launch staging block — filtered",
                    prefix, value_date.isoformat(),
                )
                continue
            # Only Overnight/1W are ever minted (_TENOR_SUFFIX) — 1M/3M and
            # the staging '7D' label are excluded by construction.
            for tenor, suffix in _TENOR_SUFFIX.items():
                rate = tenors.get(tenor)
                if rate is None:
                    logger.warning(
                        "%s %s: tenor %r missing — skipped",
                        prefix, value_date.isoformat(), tenor,
                    )
                    continue
                if not (_MIN_RATE < rate < _MAX_RATE):
                    logger.warning(
                        "%s%s %s: rate %s outside (%s, %s) — skipped",
                        prefix, suffix, value_date.isoformat(),
                        rate, _MIN_RATE, _MAX_RATE,
                    )
                    continue
                rows.append(
                    RateRow(metric_id=f"{prefix}{suffix}", as_of=value_date, value=rate)
                )
                if prefix == "dommr" and tenor == "overnight":
                    dommr_overnight_by_date[value_date] = rate
            if _TEST_DATA_TENOR in tenors:
                # A post-launch block should never carry the staging label;
                # the row is excluded by construction (only Overnight/1W are
                # ever minted) — log it so a source regression is visible.
                logger.warning(
                    "%s %s: staging tenor '7D' present in a post-launch "
                    "block — excluded", prefix, value_date.isoformat(),
                )

    for value_date, rate in dommr_overnight_by_date.items():
        rows.append(RateRow(metric_id=PARENT_METRIC_ID, as_of=value_date, value=rate))

    rows.sort(key=lambda r: (r.as_of, r.metric_id))
    return rows


def dedupe_rows(rows: list[RateRow]) -> list[RateRow]:
    """Drop duplicate (metric_id, as_of) pairs across overlapping chunk
    responses — the LAST occurrence wins (later chunks are re-reads of the
    same source rows, values identical in practice)."""
    by_key: dict[tuple[str, date], RateRow] = {}
    for r in rows:
        by_key[(r.metric_id, r.as_of)] = r
    return sorted(by_key.values(), key=lambda r: (r.as_of, r.metric_id))


def group_rows_by_date(rows: list[RateRow]) -> dict[date, list[RateRow]]:
    grouped: dict[date, list[RateRow]] = {}
    for r in rows:
        grouped.setdefault(r.as_of, []).append(r)
    return grouped


def rows_to_supabase_payload(
    day_rows: list[RateRow],
) -> tuple[dict[str, float], dict[str, date]]:
    """Per-day slice → (data, source_as_of_map) for upsert_metric_history.

    All rows in ``day_rows`` share one as_of; the per-row map still stamps
    each metric explicitly so the write can never fall back to the run date.
    """
    data = {r.metric_id: r.value for r in day_rows}
    as_of_map = {r.metric_id: r.as_of for r in day_rows}
    return data, as_of_map


# --------------------------------------------------------------------------- #
# Fetch (Playwright, stealth — I/O)
# --------------------------------------------------------------------------- #


def fetch_range_chunks(chunks: list[tuple[date, date]]) -> list[str]:
    """One browser session; per chunk, fill ``#date_picker`` and submit
    ``#search-form``, returning each rendered result document's HTML.

    Reuses fetchers/html_fetcher.py's stealth surface (launch args, UA,
    viewport, locale/timezone, playwright-stealth, F5/TSPD challenge
    detect-and-reload) — the proven recipe for bb.org.bd.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    htmls: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=list(_BROWSER_ARGS))
        context = browser.new_context(
            user_agent=_BROWSER_UA,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="Asia/Dhaka",
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)

        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
        page.wait_for_timeout(_CHALLENGE_SETTLE_MS)
        if any(m in page.content() for m in _CHALLENGE_MARKERS):
            logger.warning("F5/TSPD challenge on first visit — reloading after cookie set")
            page.reload(wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
            page.wait_for_timeout(_RESULT_SETTLE_MS)
            if any(m in page.content() for m in _CHALLENGE_MARKERS):
                raise BackfillError("challenge persisted after reload — aborting")

        for i, (chunk_start, chunk_end) in enumerate(chunks):
            if i:
                time.sleep(_CHUNK_DELAY_S)  # be gentle to a rate-limited host
            picker_value = format_date_picker_range(chunk_start, chunk_end)
            logger.info("chunk %d/%d: %s", i + 1, len(chunks), picker_value)
            # Ensure we're on the form page (after a submit we already are —
            # the POST re-renders the same page with the extra blocks).
            page.fill("#date_picker", picker_value)
            with page.expect_navigation(
                wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS
            ):
                # Submit the form directly — the datepicker widget's own JS
                # is irrelevant once the input holds the final value.
                page.eval_on_selector("#search-form", "form => form.submit()")
            page.wait_for_timeout(_RESULT_SETTLE_MS)
            html = page.content()
            if any(m in html for m in _CHALLENGE_MARKERS):
                raise BackfillError(
                    f"challenge page returned for chunk {picker_value!r} — aborting"
                )
            htmls.append(html)

        browser.close()
    return htmls


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _print_summary(rows: list[RateRow], *, sample: int = 6) -> None:
    by_metric: dict[str, int] = {}
    for r in rows:
        by_metric[r.metric_id] = by_metric.get(r.metric_id, 0) + 1
    days = sorted({r.as_of for r in rows})
    print(f"\nParsed {len(rows)} rows across {len(days)} business day(s):")
    for mid in sorted(by_metric):
        print(f"  {mid}: {by_metric[mid]} row(s)")
    if days:
        print(f"  date span: {days[0].isoformat()} .. {days[-1].isoformat()}")
    print("\nSample rows (newest first):")
    for r in sorted(rows, key=lambda r: (r.as_of, r.metric_id), reverse=True)[:sample]:
        print(f"  {r.as_of.isoformat()}  {r.metric_id:<24} {r.value}")


def run_backfill(*, start: date, end: date, dry_run: bool, chunk_days: int) -> int:
    chunks = chunk_ranges(start, end, chunk_days)
    print(
        f"Fetching {PAGE_URL}\n  window {start.isoformat()} .. {end.isoformat()} "
        f"in {len(chunks)} chunk(s) of <= {chunk_days} day(s)"
    )
    htmls = fetch_range_chunks(chunks)

    all_rows: list[RateRow] = []
    for i, html in enumerate(htmls):
        try:
            rows = parse_history_html(html)
        except Exception as e:  # ParseError included — a chunk must not die silently
            logger.error("chunk %d parse failed: %s: %s", i + 1, type(e).__name__, e)
            print(f"  chunk {i + 1} FAILED to parse: {type(e).__name__}: {e}")
            continue
        all_rows.extend(rows)
    all_rows = dedupe_rows(all_rows)

    _print_summary(all_rows)

    if dry_run:
        print("\nDRY RUN — nothing written to Supabase.")
        return 0 if all_rows else 1

    # --- Real write path ----------------------------------------------------
    from utils.supabase_writer import (
        SupabaseWriteError,
        upsert_metric_history,
        verify_landed_count,
    )

    if not all_rows:
        print("No rows parsed; nothing to upsert.")
        return 1

    # One write timestamp for the whole run so the read-back counts exactly
    # this run's rows, scoped to our ids (a sibling writer can't inflate it).
    write_ts = datetime.now(timezone.utc)
    metric_ids = sorted({r.metric_id for r in all_rows})
    total = 0
    try:
        for business_day, day_rows in sorted(group_rows_by_date(all_rows).items()):
            data, as_of_map = rows_to_supabase_payload(day_rows)
            # Landmine 22: NEVER pass url= here — that is the Supabase
            # base-URL override, not a provenance field.
            n = upsert_metric_history(
                data=data,
                as_of=business_day,
                source=SOURCE_LABEL,
                source_as_of_map=as_of_map,
                ingested_at=write_ts,
                # Header-anchored BeautifulSoup table parse — no LLM call.
                provenance="deterministic",
            )
            total += n
            logger.info("upserted %d rows for %s", n, business_day.isoformat())
    except SupabaseWriteError as e:
        print(f"Supabase write failed after {total} row(s): {e}", file=sys.stderr)
        raise
    verify_landed_count(
        total, since=write_ts, metric_ids=metric_ids, source_label="dommr_bofr_backfill"
    )
    print(f"Upserted {total} rows to metric_history.")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DOMMR/BOFR reference-rate backfill")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch + parse + print row counts and a sample; write NOTHING.")
    p.add_argument("--start", type=str, default=None,
                   help=f"Start date YYYY-MM-DD (default: launch, {LAUNCH_DATE}).")
    p.add_argument("--end", type=str, default=None,
                   help="End date YYYY-MM-DD (default: today).")
    p.add_argument("--chunk-days", type=int, default=_CHUNK_DAYS_DEFAULT,
                   help=f"Max days per POST (default {_CHUNK_DAYS_DEFAULT}).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else LAUNCH_DATE
    if start < LAUNCH_DATE:
        print(
            f"NOTE: clamping --start to launch date {LAUNCH_DATE.isoformat()} "
            "(earlier rows are staging test data and are filtered anyway).",
        )
        start = LAUNCH_DATE
    if start > end:
        print("ERROR: --start must be on or before --end", file=sys.stderr)
        return 2

    return run_backfill(
        start=start, end=end, dry_run=args.dry_run, chunk_days=args.chunk_days
    )


if __name__ == "__main__":
    sys.exit(main())
