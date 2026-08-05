"""ONE-TIME BACKFILL — historical BB reserves gross/BPM6 into metric_history_monthly.

*** NOT wired into any pipeline. NOT run in CI. NOT executed as part of the ***
*** D5 reserves-split PR that added this file. A real write needs the box's ***
*** SUPABASE_SERVICE_ROLE_KEY, which this session does not have and would  ***
*** not use even if it did -- execution is explicitly deferred to the repo ***
*** owner (Adnan), same as scripts/backfill_call_money_monthly.py's own    ***
*** "USAGE ... NEVER writes to Supabase in this mode" convention.          ***

Companion to the live writer, ``aggregate_latest._write_reserves_monthly_split``
(D5, reserves-memo-2026-08-05), which writes ONLY the current month on every
successful bb_forex scrape. This script fills in the months BEFORE this PR
shipped, so the two-line chart (The Brief's ``chartConfigs.ts``
``reservesConfig()``) doesn't start as a single point.

Fixture: ``scripts/_seed_data/bb_reserves_gross_bpm6_history.json`` --
27 months (2024-04 .. 2026-06), merged from TWO real, committed sources:
the repo's own test-suite-verified ``tests/fixtures/bb_forex_reserves.html``
(2024-04..2026-03) PLUS a fresh live capture of BB's ``econdata/intreserve``
page taken from this Mac 2026-08-05 (2026-08-05 Opus review M6 --
``scripts/_seed_data/bb_intreserve_live_2026-08-05.html``, verbatim HTML,
2024-07..2026-06). BB's page shows a rolling ~24-month window, so the fresh
capture no longer carries 2024-04..2024-06 -- both sources are kept and
merged rather than the live one replacing the old one, so no already-real
history is lost. Zero revisions were found on the 21 overlapping months
between the two captures; June 2026 (37578.0m gross) matches the D5
reserves-memo's independently-cited BB figure exactly. This is REAL data,
not hand-transcribed: the D5 reserves-memo explicitly warns against
hand-entering its own LLM-extracted tail table as a source of truth, so this
script deliberately reuses committed/captured HTML instead.

KNOWN GAP: BB has published BPM6 on this page since ~2021 (per
``scripts/seed_macro_monthly.py``'s ``fxBPM6`` KEY_MAP note), but this
fixture only reaches back to 2024-04, as far as the two committed captures
go. Extending further back to ~2021 needs an ARCHIVED/historical BB page
capture, not another live fetch -- BB's live page itself only ever shows a
rolling ~24-month window, so a live fetch alone can never reach 2021 no
matter how many times it's re-run. Deferred as a follow-up, not fabricated
here.

Writes to the SAME two ids the live writer uses
(``gross_reserves_usd_bn_monthly`` / ``net_reserves_bpm6_usd_bn_monthly``),
via ``utils.supabase_writer.upsert_metric_history_monthly`` /
``upsert_metric_definitions_monthly`` -- idempotent on (metric_id, as_of), so
running this before or after the live writer's next monthly row is safe
either way (merge-duplicates, not insert-fails-on-conflict).

USAGE (validate only — NEVER writes to Supabase without --write):
    PYTHONPATH=/path/to/econdelta /path/to/.venv/bin/python \\
        scripts/seed_reserves_monthly_bpm6.py --dry-run

    # Real write (owner-run only, needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY):
    scripts/seed_reserves_monthly_bpm6.py --write
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger("seed_reserves_monthly_bpm6")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO_ROOT / "scripts" / "_seed_data" / "bb_reserves_gross_bpm6_history.json"

GROSS_METRIC_ID = "gross_reserves_usd_bn_monthly"
BPM6_METRIC_ID = "net_reserves_bpm6_usd_bn_monthly"
SOURCE_LABEL = "bb_reserves_history_seed"

# Mirrors scrapers.bb_forex._BPM6_GROSS_RATIO_MIN/MAX (see that module for
# how the band was calibrated against real history) -- duplicated, not
# imported, so this script stays standalone (see build_definition_rows'
# docstring for why). 2026-08-05 review H2/H3.
_BPM6_GROSS_RATIO_MIN = 0.70
_BPM6_GROSS_RATIO_MAX = 0.95
_GRACE_DAYS = 45


@dataclass(frozen=True)
class ReservesMonth:
    as_of: date
    gross_usd_bn: float
    bpm6_usd_bn: float


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_reserves_months(payload: dict) -> list[ReservesMonth]:
    """Parse the fixture's {period, gross_usd_mn, bpm6_usd_mn} rows into
    ReservesMonth records, converting million -> billion USD (same divide-
    by-1000 convention as scrapers.bb_forex.parse_reserves).

    ``as_of`` is the MONTH-END date (2026-08-05 review H3), not month-start
    -- matches ``aggregate_latest._write_reserves_monthly_split``'s
    ``_month_end(reserves.reserves_date)`` exactly. The two must move
    together: if this seed used month-start while the live writer uses
    month-end, seeded history and live rows for the same calendar month
    would sit under two different ``as_of`` dates ~30 days apart in the same
    series, rather than merge-upserting the same row. Month-end also keeps
    every row inside the sentinel's monthly 45-day freshness grace for as
    long as intended -- a month-start ``as_of`` reads ~21 days/month
    "stale" for no reason (same rationale as
    ``aggregate_latest._build_tier1_source_as_of_map``'s existing month-end
    choice for the daily reserves alias).

    Applies the SAME two cross-column invariants the live parser enforces
    (D5, 2026-08-05 review H1/H2) -- a row that fails either is dropped with
    a warning, never written, exactly mirroring the parse-time refusal in
    scrapers/bb_forex.py:
      1. bpm6 < gross (direction -- a column swap).
      2. bpm6/gross inside [_BPM6_GROSS_RATIO_MIN, _BPM6_GROSS_RATIO_MAX]
         (magnitude -- a same-direction unit/decimal corruption the
         direction check alone cannot see).
    This should never trigger against the committed fixture (it's real BB
    data, verified in tests/test_seed_reserves_monthly_bpm6.py to pass both
    checks for every row), but a corrupted/hand-edited fixture must not be
    able to smuggle a bad value through this path either.
    """
    months: list[ReservesMonth] = []
    for row in payload.get("rows", []):
        period = row["period"]  # "YYYY-MM"
        year, month = int(period[:4]), int(period[5:7])
        as_of = date(year, month, monthrange(year, month)[1])
        gross_bn = float(row["gross_usd_mn"]) / 1000.0
        bpm6_bn = float(row["bpm6_usd_mn"]) / 1000.0
        if bpm6_bn >= gross_bn:
            logger.warning(
                "skipping %s: bpm6 (%.4f) >= gross (%.4f) -- column "
                "identification failure, same invariant as scrapers.bb_forex",
                period, bpm6_bn, gross_bn,
            )
            continue
        ratio = bpm6_bn / gross_bn
        if not (_BPM6_GROSS_RATIO_MIN <= ratio <= _BPM6_GROSS_RATIO_MAX):
            logger.warning(
                "skipping %s: bpm6/gross ratio %.4f outside [%.2f, %.2f] -- "
                "magnitude/unit corruption, same ratio-band check as "
                "scrapers.bb_forex",
                period, ratio, _BPM6_GROSS_RATIO_MIN, _BPM6_GROSS_RATIO_MAX,
            )
            continue
        months.append(ReservesMonth(as_of=as_of, gross_usd_bn=gross_bn, bpm6_usd_bn=bpm6_bn))
    return months


def build_history_rows(months: list[ReservesMonth]) -> list[dict]:
    """Two rows per month (gross + BPM6), matching the live writer's shape
    (aggregate_latest._write_reserves_monthly_split) exactly."""
    rows: list[dict] = []
    for m in months:
        as_of_iso = m.as_of.isoformat()
        rows.append({
            "metric_id": GROSS_METRIC_ID,
            "as_of": as_of_iso,
            "value": m.gross_usd_bn,
            "source": SOURCE_LABEL,
            "source_as_of": as_of_iso,
        })
        rows.append({
            "metric_id": BPM6_METRIC_ID,
            "as_of": as_of_iso,
            "value": m.bpm6_usd_bn,
            "source": SOURCE_LABEL,
            "source_as_of": as_of_iso,
        })
    return rows


def build_definition_rows() -> list[dict]:
    """metric_definitions_monthly rows -- identical shape (and, for
    display_name/unit/domain/notes, identical VALUES -- 2026-08-05 review
    L1) to aggregate_latest._reserves_monthly_definitions() (the live
    writer's own definitions) and to scripts/seed_macro_monthly.py's
    KEY_MAP entries for these same ids, duplicated here so this script
    stays runnable standalone without importing aggregate_latest's heavier
    dependency chain (pydantic schemas, opus_review, etc.) for two dict
    literals. grace_days=45 matches the monthly cadence tier
    (sentinel/cadence.py GRACE_DAYS_BY_CADENCE) -- v_metric_freshness
    COALESCEs it from this table, and a NULL there makes freshness
    permanently unknown for these two ids (2026-08-05 review M5)."""
    return [
        {
            "metric_id": GROSS_METRIC_ID,
            "display_name": "FX reserves (gross)",
            "unit": "USD bn",
            "source_url": "https://www.bb.org.bd/en/index.php/econdata/intreserve",
            "source_attribution": "Bangladesh Bank",
            "domain": "external",
            "description": "Gross foreign exchange reserves (BB headline measure).",
            "notes": "",
            "grace_days": _GRACE_DAYS,
        },
        {
            "metric_id": BPM6_METRIC_ID,
            "display_name": "FX reserves (BPM6/net)",
            "unit": "USD bn",
            "source_url": "https://www.bb.org.bd/en/index.php/econdata/intreserve",
            "source_attribution": "Bangladesh Bank",
            "domain": "external",
            "description": "Foreign exchange reserves per IMF BPM6 methodology.",
            "notes": "Sparse — BB began reporting BPM6 ~2021; nulls for earlier months.",
            "grace_days": _GRACE_DAYS,
        },
    ]


def _print_dry_run(rows: list[dict]) -> None:
    by_metric: dict[str, list[dict]] = {}
    for r in rows:
        by_metric.setdefault(r["metric_id"], []).append(r)
    print(f"\n=== DRY RUN — parsed rows ({len(rows)} total, NO Supabase writes) ===")
    for metric_id in sorted(by_metric):
        mrows = sorted(by_metric[metric_id], key=lambda r: r["as_of"])
        print(f"\n{metric_id}  ({len(mrows)} rows, {mrows[0]['as_of']} .. {mrows[-1]['as_of']})")
        for r in mrows[:3] + (mrows[-3:] if len(mrows) > 6 else mrows[3:]):
            print(f"    {{as_of: {r['as_of']}, value: {r['value']}}}")


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE,
                    help=f"path to the history fixture JSON (default: {DEFAULT_FIXTURE})")
    p.add_argument("--dry-run", action="store_true",
                    help="parse + build rows; print a summary; NO Supabase writes (default)")
    p.add_argument("--write", action="store_true",
                    help="perform the REAL Supabase write. Needs SUPABASE_URL + "
                         "SUPABASE_SERVICE_ROLE_KEY in the environment. Owner-run only.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    payload = load_fixture(args.fixture)
    months = build_reserves_months(payload)
    history_rows = build_history_rows(months)
    definition_rows = build_definition_rows()

    logger.info(
        "prepared %d history rows (%d months x 2 series) + %d definition rows",
        len(history_rows), len(months), len(definition_rows),
    )

    if not args.write:
        _print_dry_run(history_rows)
        logger.info(
            "--dry-run (default): no writes performed. Pass --write (with "
            "SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY set) to write for real."
        )
        return 0

    # Real write path — deliberately NOT exercised by this PR or by any test.
    # Imported lazily so --dry-run never even touches requests/env resolution.
    from utils.supabase_writer import (
        SupabaseWriteError,
        upsert_metric_definitions_monthly,
        upsert_metric_history_monthly,
    )

    try:
        sent_defs = upsert_metric_definitions_monthly(definition_rows)
        sent_hist = upsert_metric_history_monthly(history_rows)
    except SupabaseWriteError as e:
        logger.error("write failed: %s", e)
        return 1

    logger.info(
        "upsert ok: %d history rows -> metric_history_monthly, %d definitions -> "
        "metric_definitions_monthly", sent_hist, sent_defs,
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
