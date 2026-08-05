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
24 months (2024-04 .. 2026-03), extracted from the repo's own committed,
test-suite-verified fixture ``tests/fixtures/bb_forex_reserves.html`` (a
snapshot of BB's ``econdata/intreserve`` page). This is REAL data, not
hand-transcribed: the D5 reserves-memo explicitly warns against hand-entering
its own LLM-extracted tail table as a source of truth, so this script
deliberately reuses the repo's already-vetted HTML fixture instead.

KNOWN GAP: BB has published BPM6 on this page since ~2021 (per
``scripts/seed_macro_monthly.py``'s ``fxBPM6`` KEY_MAP note), but this
fixture only reaches back to 2024-04 -- as far as the committed test HTML
goes. Extending the fixture to ~2021 needs an additional live/archived BB
fetch (a fresh ``fetch_rendered_html`` + ``parse_reserves`` run, or a
manually-saved older ``intreserve`` HTML snapshot) — deferred as a follow-up,
not fabricated here.

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
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger("seed_reserves_monthly_bpm6")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO_ROOT / "scripts" / "_seed_data" / "bb_reserves_gross_bpm6_history.json"

GROSS_METRIC_ID = "gross_reserves_usd_bn_monthly"
BPM6_METRIC_ID = "net_reserves_bpm6_usd_bn_monthly"
SOURCE_LABEL = "bb_reserves_history_seed"


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

    Applies the SAME bpm6 < gross cross-column invariant the live parser
    enforces (D5) -- a row that fails it is dropped with a warning, never
    written, exactly mirroring the parse-time refusal in
    scrapers/bb_forex.py. This should never trigger against the committed
    fixture (it's real BB data), but a corrupted/hand-edited fixture must
    not be able to smuggle a column-swapped value through this path either.
    """
    months: list[ReservesMonth] = []
    for row in payload.get("rows", []):
        period = row["period"]  # "YYYY-MM"
        year_s, month_s = period.split("-")
        as_of = date(int(year_s), int(month_s), 1)
        gross_bn = float(row["gross_usd_mn"]) / 1000.0
        bpm6_bn = float(row["bpm6_usd_mn"]) / 1000.0
        if bpm6_bn >= gross_bn:
            logger.warning(
                "skipping %s: bpm6 (%.4f) >= gross (%.4f) -- column "
                "identification failure, same invariant as scrapers.bb_forex",
                period, bpm6_bn, gross_bn,
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
    """metric_definitions_monthly rows -- identical shape to
    aggregate_latest._reserves_monthly_definitions() (the live writer's own
    definitions), duplicated here so this script stays runnable standalone
    without importing aggregate_latest's heavier dependency chain (pydantic
    schemas, opus_review, etc.) for two dict literals."""
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
        },
        {
            "metric_id": BPM6_METRIC_ID,
            "display_name": "FX reserves (BPM6/net)",
            "unit": "USD bn",
            "source_url": "https://www.bb.org.bd/en/index.php/econdata/intreserve",
            "source_attribution": "Bangladesh Bank",
            "domain": "external",
            "description": "Foreign exchange reserves per IMF BPM6 methodology.",
            "notes": "BB began publishing BPM6 on this page ~2021; no history before then.",
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
