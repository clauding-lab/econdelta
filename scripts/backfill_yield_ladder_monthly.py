"""ONE-TIME BACKFILL — the 8-tenor T-bill/T-bond yield ladder, official
controller-computed values for 3 frozen monthly chart-series ids per tenor.

*** NOT wired into any pipeline. NOT run in CI. NOT executed as part of the ***
*** PR that added this file. A real write needs the box's                  ***
*** SUPABASE_SERVICE_ROLE_KEY, which this session does not have and would  ***
*** not use even if it did -- execution is explicitly deferred to the repo ***
*** owner (Adnan), same as scripts/backfill_monthly_chart_series.py's own  ***
*** "NOT executed" convention (Phase 1).                                   ***

Background (Phase 2 of the 2026-08-08 frozen-charts incident -- see
AGENT_LEARNINGS.md and AGENTS.md landmine 51): the 8 seeded yield-ladder
``metric_history_monthly`` ids (tbill_91d/182d/364d + yield_2y/5y/10y/15y/20y,
all ``_monthly`` suffixed) froze at ``as_of=2026-04-01`` -- same
seed-without-appender class as Phase 1's remittance/exports/CPI series, but a
DIFFERENT root cause: ``scrapers/bb_auction.py`` has captured ALL 8 tenors
into ``auction_results`` daily since May 2026 (a "live-but-unpromoted" data
source, not a dead one) -- nothing ever read that table and promoted it into
the monthly namespace. This script fills the May-July 2026 gap with
controller-computed values (verified against ``auction_results``, hardcoded
as pure data -- never recomputed at runtime); the companion LIVE writer,
``aggregate_latest._write_yield_ladder_monthly_append``, keeps the ladder
moving forward from the next completed month onward, append-only, ALL 8
tenors together or none (AGENTS.md landmine 51).

Values (controller-computed 2026-08-08 from auction_results -- DO NOT
"improve" these numbers; source: bb_auction, i.e.
https://www.bb.org.bd/en/index.php/monetaryactivity/treasury):

  tbill_91d_yield_monthly:    2026-05-01=10.15,   2026-06-01=9.4399, 2026-07-01=9.7949
  tbill_182d_yield_monthly:   2026-05-01=10.4085, 2026-06-01=9.7098, 2026-07-01=9.9901
  tbill_364d_yield_monthly:   2026-05-01=10.5,    2026-06-01=9.74,   2026-07-01=10.09
  yield_2y_monthly:           2026-05-01=10.728,  2026-06-01=10.43,  2026-07-01=9.7085
  yield_5y_monthly:           2026-05-01=10.78,   2026-06-01=10.3502,2026-07-01=9.7894
  yield_10y_monthly:          2026-05-01=10.9099, 2026-06-01=10.24,  2026-07-01=10.24
  yield_15y_monthly:          2026-05-01=11.0198, 2026-06-01=10.304, 2026-07-01=10.3425
  yield_20y_monthly:          2026-05-01=11.0875, 2026-06-01=10.34,  2026-07-01=10.4

Also re-points ``metric_definitions_monthly`` for these 8 ids to the real
official source -- sends FULL rows (Phase 1's H1 lesson: migration 0007
declares ``display_name``/``unit``/``domain`` ``NOT NULL`` with no
``DEFAULT``; a bulk PostgREST upsert validates the INSERT's own VALUES list
against ``NOT NULL`` BEFORE the ON CONFLICT decision runs, so a partial row
would ``23502`` the whole batch). ``display_name``/``unit``/``domain`` are
kept byte-identical to ``scripts/seed_macro_monthly.py``'s ``KEY_MAP``
entries for these 8 ids (tb91/tb182/tbill364/tr2y/tr5y/tr10y/tr15y/tr20y).

``as_of`` uses the day-1-of-data-month convention (2026-05-01 = May data),
matching every existing row already in these 8 series and the live
appender's own convention -- AGENTS.md landmine 51.

USAGE (dry-run is the DEFAULT — NEVER writes to Supabase without --write):
    PYTHONPATH=/path/to/econdelta /path/to/.venv/bin/python \\
        scripts/backfill_yield_ladder_monthly.py --dry-run

    # Real write (owner-run only, needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY):
    scripts/backfill_yield_ladder_monthly.py --write

PYTHONPATH LESSON (Phase 1 box incident, 2026-08-08): Phase 1's
``scripts/backfill_monthly_chart_series.py`` failed with
``ModuleNotFoundError`` on the box when invoked as a plain file path
(``python scripts/backfill_monthly_chart_series.py --write``) WITHOUT
``PYTHONPATH=.`` set -- Python puts the SCRIPT's own directory on
``sys.path[0]``, not the repo root, so the deferred ``from utils... import``
inside the ``--write`` branch couldn't find the ``utils`` package.
``--dry-run`` never caught it because that import is write-branch-only. This
script bootstraps ``sys.path`` itself (mirroring ``scripts/build_catalog.py``'s
own ``sys.path.insert(0, str(REPO_ROOT))`` pattern) so ``--write`` works
regardless of how it's invoked or what ``PYTHONPATH`` happens to be set to.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger("backfill_yield_ladder_monthly")

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class BackfillRow:
    metric_id: str
    as_of: date
    value: float
    source: str


# ---------------------------------------------------------------------------
# Controller-computed values (2026-08-08) — pure data, no I/O. DO NOT edit
# these numbers without a fresh controller/owner sign-off; see the module
# docstring for the source (auction_results, via bb_auction) each was
# computed from.
# ---------------------------------------------------------------------------

YIELD_SOURCE = "bb_auction"

TBILL_91D_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("tbill_91d_yield_monthly", date(2026, 5, 1), 10.15, YIELD_SOURCE),
    BackfillRow("tbill_91d_yield_monthly", date(2026, 6, 1), 9.4399, YIELD_SOURCE),
    BackfillRow("tbill_91d_yield_monthly", date(2026, 7, 1), 9.7949, YIELD_SOURCE),
)

TBILL_182D_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("tbill_182d_yield_monthly", date(2026, 5, 1), 10.4085, YIELD_SOURCE),
    BackfillRow("tbill_182d_yield_monthly", date(2026, 6, 1), 9.7098, YIELD_SOURCE),
    BackfillRow("tbill_182d_yield_monthly", date(2026, 7, 1), 9.9901, YIELD_SOURCE),
)

TBILL_364D_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("tbill_364d_yield_monthly", date(2026, 5, 1), 10.5, YIELD_SOURCE),
    BackfillRow("tbill_364d_yield_monthly", date(2026, 6, 1), 9.74, YIELD_SOURCE),
    BackfillRow("tbill_364d_yield_monthly", date(2026, 7, 1), 10.09, YIELD_SOURCE),
)

YIELD_2Y_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("yield_2y_monthly", date(2026, 5, 1), 10.728, YIELD_SOURCE),
    BackfillRow("yield_2y_monthly", date(2026, 6, 1), 10.43, YIELD_SOURCE),
    BackfillRow("yield_2y_monthly", date(2026, 7, 1), 9.7085, YIELD_SOURCE),
)

YIELD_5Y_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("yield_5y_monthly", date(2026, 5, 1), 10.78, YIELD_SOURCE),
    BackfillRow("yield_5y_monthly", date(2026, 6, 1), 10.3502, YIELD_SOURCE),
    BackfillRow("yield_5y_monthly", date(2026, 7, 1), 9.7894, YIELD_SOURCE),
)

YIELD_10Y_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("yield_10y_monthly", date(2026, 5, 1), 10.9099, YIELD_SOURCE),
    BackfillRow("yield_10y_monthly", date(2026, 6, 1), 10.24, YIELD_SOURCE),
    BackfillRow("yield_10y_monthly", date(2026, 7, 1), 10.24, YIELD_SOURCE),
)

YIELD_15Y_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("yield_15y_monthly", date(2026, 5, 1), 11.0198, YIELD_SOURCE),
    BackfillRow("yield_15y_monthly", date(2026, 6, 1), 10.304, YIELD_SOURCE),
    BackfillRow("yield_15y_monthly", date(2026, 7, 1), 10.3425, YIELD_SOURCE),
)

YIELD_20Y_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("yield_20y_monthly", date(2026, 5, 1), 11.0875, YIELD_SOURCE),
    BackfillRow("yield_20y_monthly", date(2026, 6, 1), 10.34, YIELD_SOURCE),
    BackfillRow("yield_20y_monthly", date(2026, 7, 1), 10.4, YIELD_SOURCE),
)

ALL_BACKFILL_ROWS: tuple[BackfillRow, ...] = (
    TBILL_91D_ROWS + TBILL_182D_ROWS + TBILL_364D_ROWS
    + YIELD_2Y_ROWS + YIELD_5Y_ROWS + YIELD_10Y_ROWS + YIELD_15Y_ROWS + YIELD_20Y_ROWS
)

# The EXACT (metric_id, as_of) pairs this script is allowed to touch -- 8
# tenors x 3 months. build_history_rows asserts its output never drifts from
# this set, so the script can never silently grow scope.
EXPECTED_PAIRS: frozenset[tuple[str, date]] = frozenset(
    (r.metric_id, r.as_of) for r in ALL_BACKFILL_ROWS
)
assert len(EXPECTED_PAIRS) == 24, f"expected exactly 24 backfill pairs, got {len(EXPECTED_PAIRS)}"

_AUCTION_RESULTS_URL = "https://www.bb.org.bd/en/index.php/monetaryactivity/treasury"

# metric_definitions_monthly FULL rows for the same 8 ids (Phase 1's H1
# lesson: partial rows 23502 the whole bulk upsert -- migration 0007's
# NOT NULL columns have no DEFAULT). display_name/unit/domain kept
# byte-identical to scripts/seed_macro_monthly.py's KEY_MAP entries for
# these 8 ids (tb91/tb182/tbill364/tr2y/tr5y/tr10y/tr15y/tr20y).
DEFINITION_UPDATES: tuple[dict, ...] = (
    {
        "metric_id": "tbill_91d_yield_monthly",
        "display_name": "91-day T-bill yield",
        "unit": "%",
        "source_url": _AUCTION_RESULTS_URL,
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "91-day T-bill yield",
        "notes": "",
    },
    {
        "metric_id": "tbill_182d_yield_monthly",
        "display_name": "182-day T-bill yield",
        "unit": "%",
        "source_url": _AUCTION_RESULTS_URL,
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "182-day T-bill yield",
        "notes": "",
    },
    {
        "metric_id": "tbill_364d_yield_monthly",
        "display_name": "364-day T-bill yield",
        "unit": "%",
        "source_url": _AUCTION_RESULTS_URL,
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "364-day T-bill yield",
        "notes": "",
    },
    {
        "metric_id": "yield_2y_monthly",
        "display_name": "2Y bond yield",
        "unit": "%",
        "source_url": _AUCTION_RESULTS_URL,
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "2Y bond yield",
        "notes": "",
    },
    {
        "metric_id": "yield_5y_monthly",
        "display_name": "5Y bond yield",
        "unit": "%",
        "source_url": _AUCTION_RESULTS_URL,
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "5Y bond yield",
        "notes": "",
    },
    {
        "metric_id": "yield_10y_monthly",
        "display_name": "10Y bond yield",
        "unit": "%",
        "source_url": _AUCTION_RESULTS_URL,
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "10Y bond yield",
        "notes": "",
    },
    {
        "metric_id": "yield_15y_monthly",
        "display_name": "15Y bond yield",
        "unit": "%",
        "source_url": _AUCTION_RESULTS_URL,
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "15Y bond yield",
        "notes": "",
    },
    {
        "metric_id": "yield_20y_monthly",
        "display_name": "20Y bond yield",
        "unit": "%",
        "source_url": _AUCTION_RESULTS_URL,
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "20Y bond yield",
        "notes": "",
    },
)


def build_history_rows(rows: tuple[BackfillRow, ...] = ALL_BACKFILL_ROWS) -> list[dict]:
    """Pure transform: BackfillRow tuples -> metric_history_monthly upsert dicts.

    Raises AssertionError if the built (metric_id, as_of) set is not EXACTLY
    the 24 controller-approved pairs -- a hard guard against accidental scope
    drift.
    """
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
            "backfill scope drift: built rows do not match the 24 "
            f"controller-approved (metric_id, as_of) pairs. extra={extra} missing={missing}"
        )
    return out


def build_definition_rows() -> list[dict]:
    """metric_definitions_monthly FULL rows for the 8 yield-ladder ids."""
    return [dict(d) for d in DEFINITION_UPDATES]


def _print_dry_run(rows: list[dict]) -> None:
    by_metric: dict[str, list[dict]] = {}
    for r in rows:
        by_metric.setdefault(r["metric_id"], []).append(r)
    print(f"\n=== DRY RUN — parsed rows ({len(rows)} total, NO Supabase writes) ===")
    for metric_id in sorted(by_metric):
        mrows = sorted(by_metric[metric_id], key=lambda r: r["as_of"])
        print(f"\n{metric_id}  ({len(mrows)} rows)")
        for r in mrows:
            print(f"    {{as_of: {r['as_of']}, value: {r['value']}, source: {r['source']}}}")


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument("--dry-run", action="store_true",
                    help="build rows; print a summary; NO Supabase writes (default)")
    p.add_argument("--write", action="store_true",
                    help="perform the REAL Supabase write. Needs SUPABASE_URL + "
                         "SUPABASE_SERVICE_ROLE_KEY in the environment. Owner-run only.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    history_rows = build_history_rows()
    definition_rows = build_definition_rows()

    logger.info(
        "prepared %d history rows (8 tenors x 3 months) + %d definition rows",
        len(history_rows), len(definition_rows),
    )

    if not args.write:
        _print_dry_run(history_rows)
        logger.info(
            "--dry-run (default): no writes performed. Pass --write (with "
            "SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY set) to write for real."
        )
        return 0

    # Real write path — deliberately NOT exercised by this PR or by any test
    # that asserts an actual Supabase call goes out.
    #
    # PYTHONPATH lesson (Phase 1 box incident): bootstrap sys.path with the
    # repo root BEFORE the lazy `from utils... import`, mirroring
    # scripts/build_catalog.py's own pattern -- Python puts this script's
    # OWN directory on sys.path[0] when invoked as a plain file path
    # (`python scripts/backfill_yield_ladder_monthly.py --write`), not the
    # repo root, so `import utils` would otherwise raise ModuleNotFoundError
    # unless the caller happens to have PYTHONPATH=. set. --dry-run never
    # exercises this import at all, so this bug is invisible until --write.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from utils.supabase_writer import (
        SupabaseWriteError,
        upsert_metric_definitions_monthly,
        upsert_metric_history_monthly,
    )

    # History rows BEFORE the definitions re-point (Phase 1's H1 lesson):
    # definitions are metadata-only and must never gate whether the 24
    # controller-approved values land.
    try:
        sent_hist = upsert_metric_history_monthly(history_rows)
        sent_defs = upsert_metric_definitions_monthly(definition_rows)
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
