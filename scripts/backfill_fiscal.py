"""One-time fiscal backfill: monthly govt bank borrowing + NBR revenue, and
annual ADP completion %, into Supabase ``metric_history_monthly``.

Mirrors the conventions of ``scripts/seed_macro_monthly.py`` (pure transform
functions first, then the CLI / upsert path) and reuses the same PostgREST
upsert shape (``Prefer: resolution=merge-duplicates``, ``on_conflict=metric_id,as_of``).

Sources (all reachable from a BD-egress host; MOF Oracle CDN is global):
  * MOF Monthly Fiscal Report (MFR) PDFs — discovered by headless-rendering the
    JS archive page, harvesting objectstorage.oraclecloud links, then reading
    page 1 of each PDF to learn its true report month.
      - Table 6 row "2.1 Borrowing from Banking System (Net)", single-month
        FY26 column -> govt_bank_borrow_monthly_cr  (BDT crore, as_of=month-end)
      - Table 4 row "a. NBR", single-month FY26 column -> nbr_revenue_monthly_cr
  * ADP completion % (annual) — adp_completion_pct_annual, as_of=fiscal-year-start.
    Backfilled (2026-05-30) from IMED Annual Progress Report year-end figures
    (% of revised allocation), FY21-FY25 — see ADP_VALUES. Use --adp-only to
    seed just these annual rows without fetching any MFR.

CRITICAL: ``--dry-run`` fetches + parses + prints sample rows and writes
NOTHING. There is no write path that runs without an explicit non-dry-run
invocation plus live Supabase credentials.

The MFR single-month column is the TRUE month-on-month figure, NOT an FYTD
difference. A self-check (this-month FYTD minus prior-month FYTD vs the
published single-month) flags any row that diverges by more than 5% — this
catches column-misalignment if MOF changes the table layout.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from scripts import mfr_parser as mfr

logger = logging.getLogger("backfill_fiscal")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SOURCE = "mof_mfr_backfill"
MFR_ARCHIVE_URL = (
    "https://mof.gov.bd/site/page/0f0724a1-73e5-47f5-b65f-a0c3f84381f7/"
    "Monthly-Fiscal-Reports"
)
ORACLE_CDN_HOST = "objectstorage.ap-dcc-gazipur-1.oraclecloud15.com"

# Per-fiscal-year annual-budget anchors (BDT crore), keyed by FY-END year.
# The MFR table layout is not fixed-position, so the parser locates the
# current fiscal year's annual *Budget* value (stable across all 12 monthly
# issues of that FY) and reads the single-month + FYTD figures right after it.
# Each value is read straight off the printed table and CROSS-CHECKED across
# two fiscal years' reports (every report prints both its own year's Budget
# and the prior year's). Verified 2026-06-06.
#   FY26: reproduces the live DB (borrow 5,720 / NBR 28,027 for Oct 2025).
#   FY25/FY24: confirmed in both the year's own reports and the next year's
#   prior-year column. FY23 and older are added during the dry-run (Task 8),
#   each confirmed the same way; unconfirmable years are skipped, not guessed.
FY_BORROW_BUDGET: dict[int, float] = {
    2026: 104000.0,   # Table 6 "Borrowing from Banking System (Net)" Budget FY26
    2025: 137500.0,   # Budget FY25
    2024: 132395.0,   # Budget FY24
}
FY_NBR_BUDGET: dict[int, float] = {
    2026: 499001.0,   # Table 4 "a. NBR" Budget FY26
    2025: 480000.0,   # Budget FY25
    2024: 430000.0,   # Budget FY24
}

# Self-check tolerance: |published_single_month - (fytd_t - fytd_{t-1})| / single
SELF_CHECK_TOLERANCE = 0.05

# Metric ids (target table: metric_history_monthly).
METRIC_BORROW = "govt_bank_borrow_monthly_cr"
METRIC_NBR = "nbr_revenue_monthly_cr"
METRIC_ADP = "adp_completion_pct_annual"

# ADP completion % (% of REVISED ADP allocation), annual. Backfilled
# 2026-05-30 from IMED Annual Progress Report year-end reporting, cross-checked
# against published crore amounts. Keyed by fiscal-year-END year; as_of =
# fiscal-year START (1 Jul of fy_end-1) via fiscal_year_start().
ADP_SOURCE = "IMED Annual Progress Report (% of revised ADP allocation)"
ADP_VALUES: dict[int, float] = {
    2021: 82.21,
    2022: 92.79,
    2023: 84.16,
    2024: 80.92,
    2025: 67.85,  # 49-year low (post-uprising administrative slowdown)
}


# ---------------------------------------------------------------------------
# Pure transform helpers (no I/O)
# ---------------------------------------------------------------------------


def month_end(year: int, month: int) -> date:
    """Return the last calendar day of (year, month) — the as_of for monthly rows."""
    if month == 12:
        return date(year, 12, 31)
    # day before the 1st of next month
    nxt = date(year + (month // 12), (month % 12) + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def fiscal_year_start(fy_end_year: int) -> date:
    """ADP as_of = fiscal-year START. Bangladesh FY runs Jul-Jun; 'FY25' ends
    June 2025, so it starts 1 July 2024. fy_end_year=2025 -> date(2024, 7, 1)."""
    return date(fy_end_year - 1, 7, 1)


def fiscal_year_of(year: int, month: int) -> int:
    """Return the FY-END year for a report month. Bangladesh fiscal year runs
    1 July -> 30 June, named by its end year (Jul 2025..Jun 2026 = FY26)."""
    return year + 1 if month >= 7 else year


def build_monthly_row(metric_id: str, year: int, month: int, value: float,
                      source: str = DEFAULT_SOURCE) -> dict:
    as_of = month_end(year, month).isoformat()
    return {
        "metric_id": metric_id,
        "as_of": as_of,
        "value": value,
        "source": source,
        "source_as_of": as_of,
    }


def build_adp_rows(source: str = ADP_SOURCE) -> list[dict]:
    """Annual ADP completion % rows from ADP_VALUES (static backfill).

    as_of = fiscal-year START. Idempotent on (metric_id, as_of)."""
    rows: list[dict] = []
    for fy_end, pct in sorted(ADP_VALUES.items()):
        as_of = fiscal_year_start(fy_end).isoformat()
        rows.append({
            "metric_id": METRIC_ADP,
            "as_of": as_of,
            "value": pct,
            "source": source,
            "source_as_of": as_of,
        })
    return rows


def self_check_fytd(series_by_month: dict[tuple[int, int], "ParsedMfr"],
                    which: str) -> list[str]:
    """Cross-check: for consecutive months, published single-month value
    should ~= (this FYTD - prior FYTD). Returns a list of human-readable
    warning strings for any month that diverges by > SELF_CHECK_TOLERANCE.

    ``which`` is 'borrow' or 'nbr'. The check only runs on truly consecutive
    months within the same fiscal year (skips July, the FY's first month,
    where single==FYTD by construction).
    """
    warnings: list[str] = []
    keys = sorted(series_by_month)
    for (y, m) in keys:
        if m == 7:  # fiscal-year first month: single == FYTD, nothing to diff
            continue
        prev = (y, m - 1) if m > 1 else (y - 1, 12)
        if prev not in series_by_month:
            continue  # gap — cannot cross-check
        cur = series_by_month[(y, m)]
        pre = series_by_month[prev]
        single = cur.borrow_single if which == "borrow" else cur.nbr_single
        fytd_now = cur.borrow_fytd if which == "borrow" else cur.nbr_fytd
        fytd_pre = pre.borrow_fytd if which == "borrow" else pre.nbr_fytd
        implied = fytd_now - fytd_pre
        denom = abs(single) if abs(single) > 1e-9 else 1.0
        rel = abs(single - implied) / denom
        if rel > SELF_CHECK_TOLERANCE:
            warnings.append(
                f"{which} {y}-{m:02d}: published single={single:,.0f} vs "
                f"FYTD-diff={implied:,.0f} (rel {rel:.1%} > {SELF_CHECK_TOLERANCE:.0%})"
            )
    return warnings


# ---------------------------------------------------------------------------
# Fetch / discovery (I/O)
# ---------------------------------------------------------------------------


@dataclass
class ParsedMfr:
    year: int
    month: int
    pdf_url: str
    borrow_single: float
    borrow_fytd: float
    nbr_single: float
    nbr_fytd: float


def _download(url: str, dest: Path) -> Path:
    req = Request(url, headers={"User-Agent": "EconDelta/3.0 (fiscal-backfill)"})
    with urlopen(req, timeout=90) as resp:
        body = resp.read()
    if body[:5] != b"%PDF-":
        raise RuntimeError(f"non-PDF body from {url} (first bytes {body[:8]!r})")
    dest.write_bytes(body)
    return dest


def discover_mfr_pdf_links(*, scrape_fn) -> list[str]:
    """Harvest MFR PDF links from the JS archive page.

    ``scrape_fn`` must return the firecrawl scrape response dict (so tests can
    inject a fixture). Document order ~ newest first. Dedupes, keeps only
    Oracle-CDN office-mof PDFs.
    """
    resp = scrape_fn(MFR_ARCHIVE_URL)
    links = resp.get("links", []) if isinstance(resp, dict) else []
    out: list[str] = []
    seen: set[str] = set()
    for link in links:
        if ORACLE_CDN_HOST in link and "office-mof" in link and link.lower().endswith(".pdf"):
            if link not in seen:
                seen.add(link)
                out.append(link)
    return out


def parse_one_mfr(pdf_path: str, pdf_url: str) -> ParsedMfr:
    year, month = mfr.parse_report_month(pdf_path)
    b = mfr.parse_bank_borrowing(pdf_path, fy_budget_crore=FY26_BORROW_BUDGET_CRORE)
    n = mfr.parse_nbr_revenue(pdf_path, fy_budget_crore=FY26_NBR_BUDGET_CRORE)
    borrow_fytd, nbr_fytd = b.fytd, n.fytd
    # July is the fiscal year's first month: FYTD == single-month by definition.
    # In some July issues the MFR repeats a prior column in the FYTD slot
    # (observed for the borrowing row), so we normalize FYTD to the single-month
    # value rather than trust the repeated column. This keeps the FYTD-diff
    # self-check honest for the August row that diffs against July.
    if month == 7:
        borrow_fytd = b.single_month
        nbr_fytd = n.single_month
    return ParsedMfr(
        year=year, month=month, pdf_url=pdf_url,
        borrow_single=b.single_month, borrow_fytd=borrow_fytd,
        nbr_single=n.single_month, nbr_fytd=nbr_fytd,
    )


# ---------------------------------------------------------------------------
# Supabase upsert (only reached when NOT --dry-run)
# ---------------------------------------------------------------------------

_BATCH_SIZE = 500
_DEFAULT_TIMEOUT = 60


class BackfillError(Exception):
    pass


def _resolve_credentials() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
           or os.environ.get("SUPABASE_SERVICE_KEY"))
    if not url or not key:
        raise BackfillError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SERVICE_KEY) must be set"
        )
    return url.rstrip("/"), key


def _upsert(url: str, key: str, rows: list[dict], *, table: str,
            on_conflict: str, timeout: int = _DEFAULT_TIMEOUT) -> int:
    if not rows:
        return 0
    import requests  # local import — only needed on the write path
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    endpoint = f"{url}/rest/v1/{table}?on_conflict={on_conflict}"
    sent = 0
    sess = requests.Session()
    for i in range(0, len(rows), _BATCH_SIZE):
        batch = rows[i:i + _BATCH_SIZE]
        resp = sess.post(endpoint, headers=headers, json=batch, timeout=timeout)
        if resp.status_code >= 300:
            raise BackfillError(
                f"upsert {table} failed: HTTP {resp.status_code}: {resp.text[:300]}"
            )
        sent += len(batch)
    return sent


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _firecrawl_scrape(url: str) -> dict:
    """Placeholder hook. In production this is wired to the firecrawl MCP /
    a stealth scrape that returns {'links': [...]}. For local --dry-run with a
    pre-seeded fixture, pass --links-file. See README in this scratch dir."""
    raise BackfillError(
        "no scrape backend wired; pass --links-file with one PDF URL per line "
        "(harvest via firecrawl stealth waitFor=9000 against the archive page)"
    )


def _load_links(args) -> list[str]:
    if args.links_file:
        text = Path(args.links_file).read_text()
        urls = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return [u for u in urls if ORACLE_CDN_HOST in u and u.lower().endswith(".pdf")]
    if args.pdf_url:
        return list(args.pdf_url)
    return discover_mfr_pdf_links(scrape_fn=_firecrawl_scrape)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch + parse + print sample rows; write NOTHING to Supabase")
    p.add_argument("--links-file",
                   help="file with MFR PDF URLs (one per line), e.g. harvested via firecrawl")
    p.add_argument("--pdf-url", action="append",
                   help="explicit MFR PDF URL (repeatable); bypasses discovery")
    p.add_argument("--cache-dir", default="/tmp/backfill-build/F7_fiscal/_pdfs",
                   help="where to cache downloaded PDFs")
    p.add_argument("--max-reports", type=int, default=12,
                   help="cap number of MFRs to download (newest-first)")
    p.add_argument("--adp-only", action="store_true",
                   help="seed only the annual ADP completion %% rows (no MFR fetch)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.adp_only:
        adp_rows = build_adp_rows()
        logger.info("ADP-only: %d annual rows (FY%d..FY%d)",
                    len(adp_rows), min(ADP_VALUES), max(ADP_VALUES))
        if args.dry_run:
            for r in adp_rows:
                logger.info("  %s", r)
            logger.info("=== --dry-run: %d ADP rows would upsert ===", len(adp_rows))
            return 0
        url, key = _resolve_credentials()
        sent = _upsert(url, key, adp_rows,
                       table="metric_history_monthly", on_conflict="metric_id,as_of")
        logger.info("upsert ok: %d ADP rows -> metric_history_monthly", sent)
        return 0

    links = _load_links(args)[: args.max_reports]
    if not links:
        logger.error("no MFR PDF links to process")
        return 1
    logger.info("processing %d MFR PDF link(s)", len(links))

    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    parsed: dict[tuple[int, int], ParsedMfr] = {}
    for url in links:
        name = Path(urlparse(url).path).name
        dest = cache / name
        try:
            if not dest.exists():
                _download(url, dest)
            pm = parse_one_mfr(str(dest), url)
        except Exception as e:  # noqa: BLE001 — surface, keep going
            logger.warning("skipping %s: %s", url, e)
            continue
        parsed[(pm.year, pm.month)] = pm
        logger.info(
            "  %4d-%02d  borrow single=%10.0f fytd=%10.0f | nbr single=%10.0f fytd=%10.0f",
            pm.year, pm.month, pm.borrow_single, pm.borrow_fytd, pm.nbr_single, pm.nbr_fytd,
        )

    # FYTD-diff self-checks
    for which in ("borrow", "nbr"):
        for w in self_check_fytd(parsed, which):
            logger.warning("SELF-CHECK: %s", w)

    # Build rows
    history_rows: list[dict] = []
    for (y, m), pm in sorted(parsed.items()):
        history_rows.append(build_monthly_row(METRIC_BORROW, y, m, pm.borrow_single))
        history_rows.append(build_monthly_row(METRIC_NBR, y, m, pm.nbr_single))

    logger.info("prepared %d monthly borrow/NBR rows (%d months x 2 metrics)",
                len(history_rows), len(parsed))

    # ADP annual rows (static; always appended so a full run seeds all 3 series)
    adp_rows = build_adp_rows()
    history_rows.extend(adp_rows)
    logger.info("ADP: appended %d annual rows (FY%d..FY%d)",
                len(adp_rows), min(ADP_VALUES), max(ADP_VALUES))

    if args.dry_run:
        logger.info("=== --dry-run: SAMPLE ROWS (no writes) ===")
        for r in history_rows[:8]:
            logger.info("  %s", r)
        logger.info("=== --dry-run complete: %d rows would be upserted ===", len(history_rows))
        return 0

    url, key = _resolve_credentials()
    sent = _upsert(url, key, history_rows,
                   table="metric_history_monthly", on_conflict="metric_id,as_of")
    logger.info("upsert ok: %d rows -> metric_history_monthly", sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
