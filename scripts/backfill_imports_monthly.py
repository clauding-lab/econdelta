"""ONE-TIME BACKFILL + DEFINITIONS REPOINT — imports_usd_mn_monthly's
April/May 2026 gap, plus retiring the dead macro_observer_seed source_url.

*** NOT wired into any pipeline. NOT run in CI. NOT executed as part of  ***
*** the PR that added this file. A real write needs the box's           ***
*** SUPABASE_SERVICE_ROLE_KEY, which this session does not have and     ***
*** would not use even if it did -- execution is explicitly deferred to ***
*** the repo owner (Adnan), same as every other scripts/backfill_*.py's ***
*** "NOT executed" convention.                                          ***

Background (PR-C, build-brief item 1; AGENTS.md landmine 52): imports_usd_
mn_monthly froze at as_of=2026-03-01 (2026-08-08 frozen-charts incident,
landmine 50) alongside the CPI trio/remittance/exports -- CPI/remittance
got a live appender at the time; imports and exports did not, and both
were routed to sentinel.ACCEPTED_STALE_METRIC_IDS. This PR builds the live
appender for imports too (aggregate_latest._write_macro_monthly_append's
new imports sub-path, parse_imports_c_and_f_table). This script backfills
the two months BB's MEI PDF had already published by the time the live
leg shipped (April and May 2026) so The Brief's chart doesn't jump straight
from March to whatever month the live leg first catches up to.

RE-READS THE PDF LIVE -- does not hardcode the values it writes. The
brief's April=7066.10 / May=6108.22 numbers (verified 2026-08-22 against
BB's real "2026_june.pdf" MEI issue, page 22 per the document's own
numbering) are used ONLY as a cross-check assertion against what this
script's own live fetch+parse actually returns -- if BB's PDF disagrees
with those numbers when this script runs (a revision, or the document
having moved on to a newer issue with different provisional figures),
the assertion fails LOUD rather than silently writing something an owner
never saw. This mirrors AGENT_LEARNINGS.md's "hand-verified official
values, never auto-derived" backfill philosophy while still avoiding the
worse failure mode of hand-transcribing numbers that drift the moment the
PDF is revised.

Also repoints metric_definitions_monthly.imports_usd_mn_monthly's
source_url away from the dead `macro.thenazmussakib.com` (the site that
seeded this id's original history, scripts/seed_macro_monthly.py) to BB's
own MEI publication index -- the definitions upsert is a FULL row (Phase
1's H1 lesson: migration 0007's NOT NULL columns have no DEFAULT, so a
partial row 23502s the whole batch), with display_name/unit/domain kept
byte-identical to scripts/seed_macro_monthly.py's KEY_MAP entry for this id.

USAGE (dry-run is the DEFAULT -- NEVER writes to Supabase without --write):
    PYTHONPATH=/path/to/econdelta /path/to/.venv/bin/python \\
        scripts/backfill_imports_monthly.py --dry-run

    # Real write (owner-run only, needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY):
    scripts/backfill_imports_monthly.py --write

PYTHONPATH LESSON (Phase 1/2 box incidents): this script bootstraps
sys.path with the repo root at IMPORT TIME (not lazily inside --write),
because unlike the yield-ladder/reserves backfills (pure hardcoded data,
no live parse), this script needs `from aggregate_latest import ...` for
BOTH --dry-run (to fetch+parse the live PDF) and --write -- so the
PYTHONPATH[0]-is-this-script's-own-directory footgun would otherwise bite
--dry-run too, not just --write.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

logger = logging.getLogger("backfill_imports_monthly")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aggregate_latest import (  # noqa: E402
    _IMPORTS_MONTHLY_ID,
    _IMPORTS_SOURCE,
    _fetch_imports_mei_pdf,
    parse_imports_c_and_f_table,
)

# Controller cross-check values ONLY (verified 2026-08-22 against BB's real
# "2026_june.pdf" MEI issue) -- NOT what gets written. See module docstring.
_EXPECTED_VALUES: dict[date, float] = {
    date(2026, 4, 1): 7066.10,
    date(2026, 5, 1): 6108.22,
}
_CROSS_CHECK_TOLERANCE_PCT = 0.001  # 0.1% -- these are meant to match exactly

_MEI_INDEX_URL = "https://www.bb.org.bd/en/index.php/publication/publictn/3/11"

DEFINITION_UPDATE: dict = {
    "metric_id": _IMPORTS_MONTHLY_ID,
    "display_name": "Imports",
    "unit": "USD mn",
    "source_url": _MEI_INDEX_URL,
    "source_attribution": "Bangladesh Bank",
    "domain": "external",
    "description": "Custom-based imports (c&f), monthly.",
    "notes": "",
}


def _cross_check(parsed: dict[date, float]) -> dict[date, float]:
    """Validate whichever of the two target months (April/May 2026) the
    live PDF's provisional column STILL carries against the controller-
    verified reference values, within tolerance -- raising LOUD only when
    a month IS present but has drifted (a real data problem an owner must
    review), never when a month is simply no longer available.

    Opus review round 1, H1 (blocker): the original version raised
    AssertionError whenever EITHER month was entirely missing from the
    PDF. That bricks this script the moment BB's fiscal year rolls (~Oct
    2026, aggregate_latest._imports_splice_check's docstring has the full
    mechanism): April/May 2026 move from the PDF's provisional ('P')
    column to the revised ('R') comparator column that
    parse_imports_c_and_f_table never reads, so both would be "missing"
    forever afterward -- a one-time backfill script has no business
    hard-failing on a month that has simply moved on. The orchestrator
    runs this script TODAY, well before any such roll, so this almost
    never actually triggers -- but the script must degrade gracefully
    (warn, skip that month, keep going) rather than crash if run later.

    Returns the subset of _EXPECTED_VALUES actually present AND verified
    (an empty dict is a valid, safe outcome: "nothing left to backfill
    from this source").
    """
    verified: dict[date, float] = {}
    for as_of, expected in _EXPECTED_VALUES.items():
        actual = parsed.get(as_of)
        if actual is None:
            logger.warning(
                "%s is no longer in the PDF's provisional column (FY rolled, "
                "or the document moved on to a newer issue?) -- skipping, not "
                "backfilling this month", as_of,
            )
            continue
        diff_pct = abs(actual - expected) / expected
        if diff_pct > _CROSS_CHECK_TOLERANCE_PCT:
            raise AssertionError(
                f"cross-check failed for {as_of}: live PDF says {actual}, "
                f"controller-verified value was {expected} ({diff_pct:.3%} "
                "difference) -- BB may have revised this figure since "
                "2026-08-22; re-verify before backfilling."
            )
        verified[as_of] = actual
    return verified


def build_history_rows(parsed: dict[date, float]) -> list[dict]:
    """Pure transform: the live-parsed {as_of: value} dict -> backfill rows
    for whichever of April/May 2026 the PDF still verifies (see
    _cross_check) -- 0, 1, or 2 rows are all valid outcomes. Raises
    AssertionError only when a present month has genuinely drifted from
    the controller-verified value."""
    verified = _cross_check(parsed)
    rows = []
    for as_of in sorted(verified):
        as_of_iso = as_of.isoformat()
        rows.append({
            "metric_id": _IMPORTS_MONTHLY_ID,
            "as_of": as_of_iso,
            "value": verified[as_of],
            "source": _IMPORTS_SOURCE,
            "source_as_of": as_of_iso,
        })
    return rows


def _print_dry_run(rows: list[dict]) -> None:
    print(f"\n=== DRY RUN — parsed rows ({len(rows)} total, NO Supabase writes) ===")
    for r in rows:
        print(f"    {{as_of: {r['as_of']}, value: {r['value']}, source: {r['source']}}}")
    print("\n=== DRY RUN — metric_definitions_monthly repoint ===")
    print(f"    {DEFINITION_UPDATE}")


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument("--dry-run", action="store_true",
                    help="fetch+parse the live MEI PDF; print a summary; NO Supabase writes (default)")
    p.add_argument("--write", action="store_true",
                    help="perform the REAL Supabase write. Needs SUPABASE_URL + "
                         "SUPABASE_SERVICE_ROLE_KEY in the environment. Owner-run only.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    # L3 (Opus review round 1): --dry-run is the DEFAULT regardless of
    # whether it's passed explicitly -- --write is the only flag that
    # actually changes behavior. Recomputed here (rather than trusting the
    # parsed --dry-run value) so the two flags can never disagree.
    args.dry_run = not args.write

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("fetching the live BB MEI PDF...")
    pdf_path = _fetch_imports_mei_pdf()
    logger.info("parsing %s for the 'Custom based import (c&f)' table...", pdf_path)
    parsed = dict(parse_imports_c_and_f_table(pdf_path))

    history_rows = build_history_rows(parsed)
    if history_rows:
        logger.info("prepared %d history row(s) (cross-check passed) + 1 definition update", len(history_rows))
    else:
        # Not an error (H1): every target month may simply already be past
        # the PDF's provisional column (FY rolled, or already backfilled).
        logger.warning(
            "0 history rows to backfill -- every target month in %s was "
            "either already appended elsewhere or is no longer in the PDF's "
            "provisional column. Nothing to do.", sorted(_EXPECTED_VALUES),
        )

    if args.dry_run:
        _print_dry_run(history_rows)
        logger.info(
            "--dry-run (default): no writes performed. Pass --write (with "
            "SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY set) to write for real."
        )
        return 0

    from utils.supabase_writer import (
        SupabaseWriteError,
        upsert_metric_definitions_monthly,
        upsert_metric_history_monthly,
    )

    try:
        sent_hist = upsert_metric_history_monthly(history_rows)
        sent_defs = upsert_metric_definitions_monthly([DEFINITION_UPDATE])
    except SupabaseWriteError as e:
        logger.error("write failed: %s", e)
        return 1

    logger.info(
        "upsert ok: %d history row(s) -> metric_history_monthly, %d definition(s) -> "
        "metric_definitions_monthly", sent_hist, sent_defs,
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
