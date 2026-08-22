"""ONE-TIME BACKFILL — July 2026 CPI trio (the month the source repoint
unblocked, PR-C build-brief item 2).

*** NOT wired into any pipeline. NOT run in CI. NOT executed as part of  ***
*** the PR that added this file. A real write needs the box's           ***
*** SUPABASE_SERVICE_ROLE_KEY, which this session does not have and     ***
*** would not use even if it did -- execution is explicitly deferred to ***
*** the repo owner (Adnan), same as scripts/backfill_monthly_chart_      ***
*** series.py's own "NOT executed" convention.                          ***

Background: AGENTS.md landmine 50's live appender (aggregate_latest.
_write_macro_monthly_append's CPI-trio sub-path) derives cpi_12m_avg_
monthly/cpi_p2p_food_monthly/cpi_p2p_nonfood_monthly from EconDelta's own
DAILY general_inflation/food_inflation/non_food_inflation ids -- which,
until this PR, were fed from BB's monthly MEI PDF bulletin (config-
conversion batch 2, landmine 49's dynamic-row blocker) and never actually
recovered July 2026's month-end vintage (the MEI PDF itself was still
one issue behind on the day this shipped -- see AGENT_LEARNINGS.md's
2026-08-22 entry). PR-C repoints general_inflation/point_to_point_
inflation to BB's live econdata/inflation HTML page (parsers/html_dated_
table_row.py), which already had July live -- the live appender will pick
July up naturally on its NEXT run once the daily ids reflect it, but this
script backfills the gap immediately rather than waiting for that next
scheduled run.

Values (verified live/derived 2026-08-22):

  cpi_12m_avg_monthly:     2026-07-01 = 8.66  (source: bb_inflation_page --
                           read directly off the "Monthly Average(Twelve
                           Month)" row of BB's econdata/inflation page, the
                           SAME source label Phase 1's scripts/backfill_
                           monthly_chart_series.py already uses for this
                           trio)
  cpi_p2p_food_monthly:    2026-07-01 = 7.16  (source:
                           derived_implied_weight_bb_inflation -- Opus
                           review round 1, M6: this figure is NOT read off
                           any BB page directly. BB's econdata/inflation
                           page carries no food/non-food split at all; 7.16
                           was SETTLED BY ARITHMETIC (the PR-C build brief's
                           source-scout pass): an implied food/general
                           weight w=0.4455 was backed out of June 2026's
                           already-known general/food/non-food triple, then
                           applied to July's known general (8.32) to solve
                           for food. The prior source label here
                           ("bb_inflation_page") was misleading -- it reads
                           as "captured verbatim from BB's page", which
                           this value was never was. OWNER FLAG: this is a
                           derived estimate, not a BB-published figure; if
                           BB or BBS later publishes July's real food/
                           non-food split, this row should be corrected to
                           the real figure and re-sourced.
  cpi_p2p_nonfood_monthly: 2026-07-01 = 9.28  (source: bb_inflation_page --
                           kept as-is; the source scout's brief did not
                           name non-food as arithmetic-derived the way it
                           did food, but non-food is subject to the SAME
                           "BB's page carries no food/non-food split"
                           caveat as food above and was not independently
                           verified against a live page either. Left
                           unchanged here because the round-1 review named
                           ONLY food explicitly (M6) -- flagged as a
                           related, not-yet-relabeled concern for the
                           owner's attention in the same PR body note.)

The food/non-food split still ultimately needs the MEI PDF (or BBS) once
that catches up to July -- this backfill does not change how the ONGOING
live appender derives those two ids going forward; it only fills the one
month the source repoint's timing gap left behind. cpi_12m_avg_monthly IS
independently re-derivable from the new live HTML source going forward
(general_inflation's own daily rows), so only THIS month needed a manual
value for it too, to avoid waiting for the next scheduled aggregate run.

``as_of`` uses the day-1-of-data-month convention (2026-07-01 = July data),
matching every existing row in this series and the live appender's own
convention (AGENTS.md landmine 50).

metric_definitions_monthly for this trio was ALREADY repointed to
econdata/inflation by Phase 1 (scripts/backfill_monthly_chart_series.py) --
no definitions change needed here.

USAGE (dry-run is the DEFAULT — NEVER writes to Supabase without --write):
    PYTHONPATH=/path/to/econdelta /path/to/.venv/bin/python \\
        scripts/backfill_cpi_july_2026.py --dry-run

    # Real write (owner-run only, needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY):
    scripts/backfill_cpi_july_2026.py --write
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger("backfill_cpi_july_2026")

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class BackfillRow:
    metric_id: str
    as_of: date
    value: float
    source: str


CPI_SOURCE = "bb_inflation_page"
# Opus review round 1, M6: distinct source label for a value that is NOT
# read off any BB page -- 7.16 was backed out arithmetically from June
# 2026's known general/food/non-food triple's implied weight, applied to
# July's known general reading. See the module docstring for the full
# derivation and the owner flag this carries.
CPI_FOOD_DERIVED_SOURCE = "derived_implied_weight_bb_inflation"
JULY_2026 = date(2026, 7, 1)

# Controller-verified values (2026-08-22) -- pure data, no I/O. DO NOT edit
# these numbers without a fresh controller/owner sign-off; see the module
# docstring for how each was verified.
ALL_BACKFILL_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("cpi_12m_avg_monthly", JULY_2026, 8.66, CPI_SOURCE),
    BackfillRow("cpi_p2p_food_monthly", JULY_2026, 7.16, CPI_FOOD_DERIVED_SOURCE),
    BackfillRow("cpi_p2p_nonfood_monthly", JULY_2026, 9.28, CPI_SOURCE),
)

# The EXACT (metric_id, as_of) pairs this script is allowed to touch -- 3
# ids x 1 month. build_history_rows asserts its output never drifts from
# this set (mirrors Phase 1/2's own scope-drift guard).
EXPECTED_PAIRS: frozenset[tuple[str, date]] = frozenset(
    (r.metric_id, r.as_of) for r in ALL_BACKFILL_ROWS
)
assert len(EXPECTED_PAIRS) == 3, f"expected exactly 3 backfill pairs, got {len(EXPECTED_PAIRS)}"


def build_history_rows(rows: tuple[BackfillRow, ...] = ALL_BACKFILL_ROWS) -> list[dict]:
    """Pure transform: BackfillRow tuples -> metric_history_monthly upsert
    dicts. Raises AssertionError if the built (metric_id, as_of) set is not
    EXACTLY the 3 controller-approved pairs."""
    out: list[dict] = []
    seen: set[tuple[str, date]] = set()
    for r in rows:
        as_of_iso = r.as_of.isoformat()
        out.append({
            "metric_id": r.metric_id,
            "as_of": as_of_iso,
            "value": r.value,
            "source": r.source,
            "source_as_of": as_of_iso,
        })
        seen.add((r.metric_id, r.as_of))
    if seen != EXPECTED_PAIRS:
        extra = seen - EXPECTED_PAIRS
        missing = EXPECTED_PAIRS - seen
        raise AssertionError(
            "backfill scope drift: built rows do not match the 3 "
            f"controller-approved (metric_id, as_of) pairs. extra={extra} missing={missing}"
        )
    return out


def _print_dry_run(rows: list[dict]) -> None:
    print(f"\n=== DRY RUN — parsed rows ({len(rows)} total, NO Supabase writes) ===")
    for r in rows:
        print(f"    {{metric_id: {r['metric_id']}, as_of: {r['as_of']}, value: {r['value']}, source: {r['source']}}}")


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument("--dry-run", action="store_true",
                    help="build rows; print a summary; NO Supabase writes (default)")
    p.add_argument("--write", action="store_true",
                    help="perform the REAL Supabase write. Needs SUPABASE_URL + "
                         "SUPABASE_SERVICE_ROLE_KEY in the environment. Owner-run only.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    # L3 (Opus review round 1): --dry-run is the DEFAULT regardless of
    # whether it's passed explicitly -- --write is the only flag that
    # actually changes behavior. Recomputed here so the two flags can
    # never disagree.
    args.dry_run = not args.write

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    history_rows = build_history_rows()
    logger.info("prepared %d history row(s) (3 ids x 1 month)", len(history_rows))

    if args.dry_run:
        _print_dry_run(history_rows)
        logger.info(
            "--dry-run (default): no writes performed. Pass --write (with "
            "SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY set) to write for real."
        )
        return 0

    # PYTHONPATH lesson (Phase 1/2 box incidents): bootstrap sys.path with
    # the repo root BEFORE the lazy `from utils... import` -- Python puts
    # this script's OWN directory on sys.path[0] when invoked as a plain
    # file path, not the repo root.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from utils.supabase_writer import SupabaseWriteError, upsert_metric_history_monthly

    try:
        sent_hist = upsert_metric_history_monthly(history_rows)
    except SupabaseWriteError as e:
        logger.error("write failed: %s", e)
        return 1

    logger.info("upsert ok: %d history row(s) -> metric_history_monthly", sent_hist)
    return 0


if __name__ == "__main__":
    sys.exit(run())
