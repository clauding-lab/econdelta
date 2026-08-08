"""ONE-TIME BACKFILL — official values for 5 frozen monthly chart-series ids.

*** NOT wired into any pipeline. NOT run in CI. NOT executed as part of the ***
*** PR that added this file. A real write needs the box's                  ***
*** SUPABASE_SERVICE_ROLE_KEY, which this session does not have and would  ***
*** not use even if it did -- execution is explicitly deferred to the repo ***
*** owner (Adnan), same as scripts/seed_reserves_monthly_bpm6.py's own     ***
*** "NOT executed" convention.                                             ***

Background (2026-08-08 frozen-charts incident, see AGENT_LEARNINGS.md and
AGENTS.md landmine 50): these ``metric_history_monthly`` chart series were
seeded ONCE from a dead third-party site (``scripts/seed_macro_monthly.py``,
source label ``macro_observer_seed``) and froze at ``as_of=2026-03-01``
because no live writer was ever built for them. The Brief's charts and the
EconDelta PWA's /macro tab both read this table -- a frozen chart hid inside
a 41-item freshness digest for 5 months.

This script fills the April-June 2026 gap with OWNER-APPROVED (2026-08-08),
VERIFIED-AGAINST-OFFICIAL-SOURCE values for 5 metric_ids x 3 months = 15
rows -- exactly 15, enforced by an assertion in ``build_history_rows`` (this
script must never silently grow its own scope). The companion LIVE writer,
``aggregate_latest._write_macro_monthly_append``, keeps these 5 series
(3 of the 5 -- the CPI trio -- plus remittance; ``exports_usd_mn_monthly``
and ``imports_usd_mn_monthly`` have NO live writer and are accepted-stale,
see sentinel/freshness.py) moving forward from July 2026 onward, and is
strictly APPEND-ONLY -- it checks ``metric_history_monthly`` before writing
and will never clobber the official values backfilled here.

Values (owner-approved 2026-08-08 -- DO NOT "improve" these numbers):

  remittance_usd_mn_monthly (source: bb_wageremitance -- BB's official
  monthly wage-remittance webpage table, https://www.bb.org.bd/en/index.php
  /econdata/wageremitance -- cross-checked live 2026-08-08, exact match):
    2026-04-01 = 3127.30, 2026-05-01 = 3442.58, 2026-06-01 = 2816.96

  exports_usd_mn_monthly (source: epb_bss -- official EPB figures via BSS):
    2026-04-01 = 4009.93, 2026-05-01 = 4402.78, 2026-06-01 = 4202.69

  cpi_12m_avg_monthly (source: bb_inflation_page):
    2026-04-01 = 8.59, 2026-05-01 = 8.63, 2026-06-01 = 8.68

  cpi_p2p_food_monthly (source: bb_inflation_page):
    2026-04-01 = 8.39, 2026-05-01 = 9.06, 2026-06-01 = 8.60

  cpi_p2p_nonfood_monthly (source: bb_inflation_page):
    2026-04-01 = 9.57, 2026-05-01 = 9.71, 2026-06-01 = 9.61

Also re-points ``metric_definitions_monthly.source_url``/``source_attribution``
for these 5 ids to the real official sources -- the seeded definitions still
point at the dead macro-observer site. Sends FULL rows (2026-08-08 Opus
review H1: migration ``0007_metric_definitions_monthly.sql`` declares
``display_name``/``unit``/``domain`` ``NOT NULL`` with no ``DEFAULT``; a
bulk PostgREST upsert is one ``INSERT ... ON CONFLICT DO UPDATE`` statement
and Postgres validates the INSERT's own VALUES list against ``NOT NULL``
BEFORE the ``ON CONFLICT`` decision runs -- a 3-key partial row would
``23502`` the WHOLE bulk upsert, not just silently skip those two columns).
``display_name``/``unit``/``domain``/``notes`` are kept byte-identical to
``scripts/seed_macro_monthly.py``'s ``KEY_MAP`` entries for these 5 ids --
only ``source_url``/``source_attribution``/``description`` change.
``run()``'s ``--write`` path posts history rows BEFORE the definitions
re-point (also H1) -- the definitions upsert is metadata-only and does not
gate whether the 15 history values land.

``as_of`` uses the day-1-of-data-month convention (2026-04-01 = April data),
matching every existing row already in these 5 series (seeded by
``scripts/seed_macro_monthly.py``'s ``normalise_as_of``) and the live
appender's own convention -- AGENTS.md landmine 50.

USAGE (dry-run is the DEFAULT — NEVER writes to Supabase without --write):
    PYTHONPATH=/path/to/econdelta /path/to/.venv/bin/python \\
        scripts/backfill_monthly_chart_series.py --dry-run

    # Real write (owner-run only, needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY):
    scripts/backfill_monthly_chart_series.py --write
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger("backfill_monthly_chart_series")


@dataclass(frozen=True)
class BackfillRow:
    metric_id: str
    as_of: date
    value: float
    source: str


# ---------------------------------------------------------------------------
# Owner-approved values (2026-08-08) — pure data, no I/O. DO NOT edit these
# numbers without a fresh owner sign-off; see the module docstring for the
# source each series was verified against.
# ---------------------------------------------------------------------------

REMITTANCE_SOURCE = "bb_wageremitance"
EXPORTS_SOURCE = "epb_bss"
CPI_SOURCE = "bb_inflation_page"

REMITTANCE_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("remittance_usd_mn_monthly", date(2026, 4, 1), 3127.30, REMITTANCE_SOURCE),
    BackfillRow("remittance_usd_mn_monthly", date(2026, 5, 1), 3442.58, REMITTANCE_SOURCE),
    BackfillRow("remittance_usd_mn_monthly", date(2026, 6, 1), 2816.96, REMITTANCE_SOURCE),
)

EXPORTS_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("exports_usd_mn_monthly", date(2026, 4, 1), 4009.93, EXPORTS_SOURCE),
    BackfillRow("exports_usd_mn_monthly", date(2026, 5, 1), 4402.78, EXPORTS_SOURCE),
    BackfillRow("exports_usd_mn_monthly", date(2026, 6, 1), 4202.69, EXPORTS_SOURCE),
)

CPI_12M_AVG_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("cpi_12m_avg_monthly", date(2026, 4, 1), 8.59, CPI_SOURCE),
    BackfillRow("cpi_12m_avg_monthly", date(2026, 5, 1), 8.63, CPI_SOURCE),
    BackfillRow("cpi_12m_avg_monthly", date(2026, 6, 1), 8.68, CPI_SOURCE),
)

CPI_P2P_FOOD_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("cpi_p2p_food_monthly", date(2026, 4, 1), 8.39, CPI_SOURCE),
    BackfillRow("cpi_p2p_food_monthly", date(2026, 5, 1), 9.06, CPI_SOURCE),
    BackfillRow("cpi_p2p_food_monthly", date(2026, 6, 1), 8.60, CPI_SOURCE),
)

CPI_P2P_NONFOOD_ROWS: tuple[BackfillRow, ...] = (
    BackfillRow("cpi_p2p_nonfood_monthly", date(2026, 4, 1), 9.57, CPI_SOURCE),
    BackfillRow("cpi_p2p_nonfood_monthly", date(2026, 5, 1), 9.71, CPI_SOURCE),
    BackfillRow("cpi_p2p_nonfood_monthly", date(2026, 6, 1), 9.61, CPI_SOURCE),
)

ALL_BACKFILL_ROWS: tuple[BackfillRow, ...] = (
    REMITTANCE_ROWS + EXPORTS_ROWS + CPI_12M_AVG_ROWS + CPI_P2P_FOOD_ROWS + CPI_P2P_NONFOOD_ROWS
)

# The EXACT (metric_id, as_of) pairs this script is allowed to touch -- 5 ids
# x 3 months. build_history_rows asserts its output never drifts from this
# set, so the script can never silently grow scope to a 16th row.
EXPECTED_PAIRS: frozenset[tuple[str, date]] = frozenset(
    (r.metric_id, r.as_of) for r in ALL_BACKFILL_ROWS
)
assert len(EXPECTED_PAIRS) == 15, f"expected exactly 15 backfill pairs, got {len(EXPECTED_PAIRS)}"

# metric_definitions_monthly re-point for the same 5 ids -- FULL rows
# (2026-08-08 Opus review H1: migration 0007 declares display_name/unit/
# domain NOT NULL with no DEFAULT; a bulk PostgREST upsert is a single
# INSERT ... ON CONFLICT DO UPDATE statement, and Postgres validates the
# INSERT's own VALUES list against NOT NULL constraints BEFORE the ON
# CONFLICT decision -- ON CONFLICT DO UPDATE does not bypass that check on
# the attempted new row. A 3-key partial row {metric_id, source_url,
# source_attribution} would 23502 the WHOLE bulk upsert, and because
# --write calls upsert_metric_definitions_monthly BEFORE
# upsert_metric_history_monthly (see run(), also fixed by H1), that failure
# would have blocked every history row from landing too).
#
# display_name/unit/domain/notes are kept BYTE-IDENTICAL to
# scripts/seed_macro_monthly.py's KEY_MAP entries for these same 5 ids
# (remUsd/expUsd/gen12M/foodP2P/nonFoodP2P) -- only source_url/
# source_attribution/description differ from the dead-site seed. This
# mirrors aggregate_latest._reserves_monthly_definitions()'s own
# byte-identical-labels convention (landmine 44 review L1).
DEFINITION_SOURCE_UPDATES: tuple[dict, ...] = (
    {
        "metric_id": "remittance_usd_mn_monthly",
        "display_name": "Remittance",
        "unit": "USD mn",
        "source_url": "https://www.bb.org.bd/en/index.php/econdata/wageremitance",
        "source_attribution": "Bangladesh Bank",
        "domain": "external",
        "description": "Remittance",
        "notes": "",
    },
    {
        "metric_id": "exports_usd_mn_monthly",
        "display_name": "Exports",
        "unit": "USD mn",
        "source_url": "https://epb.gov.bd/",
        "source_attribution": "Export Promotion Bureau (EPB) via BSS",
        "domain": "external",
        "description": "Exports",
        "notes": "",
    },
    {
        "metric_id": "cpi_12m_avg_monthly",
        "display_name": "CPI 12-month average",
        "unit": "%",
        "source_url": "https://www.bb.org.bd/en/index.php/econdata/inflation",
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "CPI 12-month average",
        "notes": "",
    },
    {
        "metric_id": "cpi_p2p_food_monthly",
        "display_name": "CPI YoY (food)",
        "unit": "%",
        "source_url": "https://www.bb.org.bd/en/index.php/econdata/inflation",
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "CPI YoY (food)",
        "notes": "",
    },
    {
        "metric_id": "cpi_p2p_nonfood_monthly",
        "display_name": "CPI YoY (non-food)",
        "unit": "%",
        "source_url": "https://www.bb.org.bd/en/index.php/econdata/inflation",
        "source_attribution": "Bangladesh Bank",
        "domain": "prices_policy",
        "description": "CPI YoY (non-food)",
        "notes": "",
    },
)


def build_history_rows(rows: tuple[BackfillRow, ...] = ALL_BACKFILL_ROWS) -> list[dict]:
    """Pure transform: BackfillRow tuples -> metric_history_monthly upsert dicts.

    Raises AssertionError if the built (metric_id, as_of) set is not EXACTLY
    the 15 owner-approved pairs -- a hard guard against accidental scope
    drift (e.g. a copy-paste duplicate row, or an extra row added later
    without updating EXPECTED_PAIRS).
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
            "backfill scope drift: built rows do not match the 15 "
            f"owner-approved (metric_id, as_of) pairs. extra={extra} missing={missing}"
        )
    return out


def build_definition_rows() -> list[dict]:
    """metric_definitions_monthly PARTIAL rows -- source_url/source_attribution
    only, for the same 5 ids. See module docstring for why this is partial."""
    return [dict(d) for d in DEFINITION_SOURCE_UPDATES]


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
        "prepared %d history rows (5 ids x 3 months) + %d definition source-updates",
        len(history_rows), len(definition_rows),
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

    # 2026-08-08 Opus review H1: history rows BEFORE the definitions
    # re-point. The definitions upsert is metadata-only (source_url/
    # source_attribution) and must never gate whether the 15 owner-approved
    # values land -- if it were to fail for any reason, the history rows
    # (the actual fix for the frozen charts) should still be written.
    try:
        sent_hist = upsert_metric_history_monthly(history_rows)
        sent_defs = upsert_metric_definitions_monthly(definition_rows)
    except SupabaseWriteError as e:
        logger.error("write failed: %s", e)
        return 1

    logger.info(
        "upsert ok: %d history rows -> metric_history_monthly, %d definition "
        "source-updates -> metric_definitions_monthly", sent_hist, sent_defs,
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
