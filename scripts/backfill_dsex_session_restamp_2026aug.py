"""ONE-TIME PRODUCTION BACKFILL — repair the July/August 2026 DSE daily-index
session mislabel (fix/dsex-session-restamp).

Root cause
----------
The 10 DSE daily ids (``dsex``, ``dsex_change``, ``dsex_change_pct``, ``ds30``,
``dses``, ``turnover_crore``, ``total_trades``, ``advancing``, ``declining``,
``unchanged`` — the exact set ``aggregate_latest.flatten_data`` writes for the
DSE snapshot) are written as one ``metric_history`` row per id, all ten
sharing the SAME ``(as_of, ingested_at)`` per run — a "cohort". A next-day
appender bug meant that, for most cohorts stored under ``as_of`` 2026-07-13
through 2026-08-20, the VALUES actually belong to the PREVIOUS trading
session, not the date they're filed under. Some of those mislabeled cohorts
are also flat-out duplicates of a correctly-dated cohort that already exists
for the true session; a few sessions were never captured under their own
correct date at all.

Verification method
--------------------
Proven 2026-08-24 ~23:45 BDT against dsebd.org's official market_summary
archive (exact archive query URL not preserved by the upstream verification
pass that produced the embedded table below — cite the DOMAIN, not a
specific path, until a fresh capture records the literal URL), fetched from
a Bangladesh-based IP address (dsebd.org rate-limits/blocks some non-BD
egress). The close-minus-change chain (``dsex[d] - dsex_chg[d] == dsex[d-1]``)
is internally consistent across all 37 trading days the archive covers,
2026-07-02 -> 2026-08-24. This script embeds 2026-07-02 through 2026-08-23
ONLY (36 sessions) — 2026-08-24 is deliberately excluded because tonight's
regular daily appender writes that session itself; this backfill has nothing
to correct there (yet).

Verified correction plan (computed, not hardcoded — see below)
----------------------------------------------------------------
Matching each stored cohort to its TRUE session by comparing its ``dsex``
value against the embedded official closes (tolerance 0.01, must match
EXACTLY ONE session) yields:

  * 4 DELETEs  (x ~10 rows each) — a stored cohort whose true session
    already has a correctly-dated twin elsewhere in the table, so the
    mislabeled copy is pure duplication, not a shift.
  * 23 RESTAMPs (x ~10 rows each) — UPDATE ``as_of`` from the stored
    (wrong) date to the true session, executed in ASCENDING stored-``as_of``
    order so each move lands in a slot a strictly-earlier action in the SAME
    pass has already vacated.
  * 3 INSERTs — sessions the appender bug never captured under any date at
    all (2026-07-13, 2026-08-11, 2026-08-20); their ``advancing`` /
    ``declining`` / ``unchanged`` are OMITTED — not present on the archive
    page, an honest gap, not a guess.

This module does NOT hardcode that plan as the executor. ``compute_plan()``
derives it from scratch, at runtime, from whatever cohorts are actually in
the database plus the embedded official table (see "Plan algorithm" below).
``EXPECTED_DELETES`` / ``EXPECTED_RESTAMPS`` / ``EXPECTED_INSERT_DATES``
below are a HARD TRIPWIRE the computed plan is cross-checked against before
any write — any action NOT a member of that hand-verified superset aborts
the run rather than silently doing something the controller didn't verify.
A computed plan with FEWER actions than the full superset is not an error:
that's the expected shape of an idempotent re-run on an already-healed
table (zero actions, exit 0) or a resumed run after a mid-sequence crash
(whatever an earlier partial ``--write`` already committed is simply no
longer in the DB to act on). ``cross_check_plan()`` accordingly checks
"every computed action is expected" (a subset check, plus order for
restamps), not "every expected action was computed."

Plan algorithm (``compute_plan``)
----------------------------------
Every stored cohort's own ``as_of`` slot starts "unresolved". Cohorts are
walked in ascending stored-``as_of`` order (matching the real bug's shape:
every genuine shift moves a cohort to an EARLIER true session, so by the
time a cohort's target slot is needed, an earlier iteration of this SAME
pass has always already resolved it):

  * ``true_session == stored as_of``            -> no action; slot marked
    permanently occupied.
  * ``true_session != stored as_of`` (a "mover"), target slot already
    permanently occupied (by a same-``dsex`` twin, guaranteed by the match)
    -> DELETE (pure duplicate).
  * mover, target slot vacated or never occupied -> RESTAMP into it; slot
    now marked permanently occupied, and the mover's OWN old slot is marked
    vacated for a later cohort to land in.
  * mover, target slot still UNRESOLVED (would only happen for a forward
    shift, or an ordering violation — never in the real data) -> abort. This
    is what the "occupied-slot" tripwire in the module's brief describes:
    the only way it is safe to land inside an occupied slot is if that
    occupant's OWN fate (delete or move-away) was already decided by an
    earlier step of this exact pass.

After the pass, any embedded official session whose date never ends up
permanently occupied is a genuine 3-way INSERT candidate.

Usage — dry-run is the DEFAULT, --write executes for real
-------------------------------------------------------------
    PYTHONPATH=/path/to/econdelta /path/to/.venv/bin/python \\
        scripts/backfill_dsex_session_restamp_2026aug.py --dry-run

    # Real write (owner-run only; needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
    # in the environment — NEVER hardcoded, see AGENTS.md landmine 18):
    scripts/backfill_dsex_session_restamp_2026aug.py --write

PYTHONPATH landmine (mirrors scripts/backfill_dse_dayend.py's own documented
usage): this script does ``from utils... import`` at module load time.
Python puts the SCRIPT's own directory on ``sys.path[0]`` when it's invoked
as a bare file path, not the repo root, so a plain
``python scripts/backfill_....py`` (no ``PYTHONPATH=``) fails with
``ModuleNotFoundError: No module named 'utils'``. Always run it with
``PYTHONPATH=/path/to/econdelta`` (or ``PYTHONPATH=.`` from the repo root)
as shown above. Under ``pytest``, this is NOT needed — ``tests/__init__.py``
makes pytest insert the repo root onto ``sys.path`` on its own (the same
reason ``tests/test_backfill_dse_dayend.py`` imports
``scripts.backfill_dse_dayend`` with no ``PYTHONPATH`` in CI).

Expected counts (this run, on an untouched database)
--------------------------------------------------------
    4 deletes x ~10 rows, 23 restamps x ~10 rows, 3 inserts (7 ids each —
    advancing/declining/unchanged omitted) = ~397 rows touched total
    (exact per-cohort row counts vary — some historical cohorts are missing
    an id or two; the script trusts what it actually reads, never a
    hardcoded 10).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import requests

from utils.supabase_writer import SupabaseWriteError, upsert_metric_history

logger = logging.getLogger("backfill_dsex_session_restamp_2026aug")

# ============================================================================
# Embedded ground truth — dsebd.org's official market_summary archive.
# Fetched from a Bangladesh-based IP, 2026-08-24 ~23:45 BDT (2026-08-24
# ~17:45 UTC). Covers 2026-07-02 .. 2026-08-23 ONLY (36 trading sessions) —
# 2026-08-24 is deliberately excluded; tonight's regular daily appender
# writes that session on its own, this backfill has nothing to correct
# there. Values are exactly as captured; do not hand-edit without a fresh
# controller/owner-verified re-fetch (mirrors backfill_cpi_july_2026.py's
# "controller-verified, do not edit" convention for its own embedded table).
#
#   dsex      — DSEX close
#   dsex_chg  — DSEX point change vs the prior session
#   ds30      — DS30 close
#   ds30_chg  — DS30 point change vs the prior session (not written by this
#               script — metric_history has no ds30_change id — kept here
#               only as part of the archive's own row shape / audit trail)
#   dses      — DSES close; null for EVERY session in this archive capture
#               (the market_summary archive page does not carry it) — an
#               honest gap, not a scrape failure; see build_insert_rows().
#   value_mn  — Total Value, Taka (millions)
#   trades    — Total number of trades
# ============================================================================
OFFICIAL_SESSIONS_RAW: dict[str, dict[str, float | None]] = {
    "2026-08-23": {"dsex": 5722.21464, "dsex_chg": -63.8659, "ds30": 2145.48199, "ds30_chg": -17.03762, "dses": None, "value_mn": 7111.497, "trades": 204263.0},
    "2026-08-20": {"dsex": 5786.08054, "dsex_chg": 16.36974, "ds30": 2162.51961, "ds30_chg": -0.4475, "dses": None, "value_mn": 6719.891, "trades": 183066.0},
    "2026-08-19": {"dsex": 5769.7108, "dsex_chg": -3.91868, "ds30": 2162.96711, "ds30_chg": -1.5008, "dses": None, "value_mn": 7328.318, "trades": 205886.0},
    "2026-08-18": {"dsex": 5773.62948, "dsex_chg": -40.64021, "ds30": 2164.46791, "ds30_chg": -11.19326, "dses": None, "value_mn": 9981.092, "trades": 247783.0},
    "2026-08-17": {"dsex": 5814.26969, "dsex_chg": -45.70984, "ds30": 2175.66117, "ds30_chg": -8.56171, "dses": None, "value_mn": 9948.55, "trades": 261007.0},
    "2026-08-16": {"dsex": 5859.97953, "dsex_chg": -23.93386, "ds30": 2184.22288, "ds30_chg": -8.51198, "dses": None, "value_mn": 11307.671, "trades": 281665.0},
    "2026-08-13": {"dsex": 5883.91339, "dsex_chg": -13.36083, "ds30": 2192.73486, "ds30_chg": -7.76119, "dses": None, "value_mn": 9256.692, "trades": 239663.0},
    "2026-08-12": {"dsex": 5897.27422, "dsex_chg": -6.25684, "ds30": 2200.49605, "ds30_chg": -4.07332, "dses": None, "value_mn": 10737.306, "trades": 267674.0},
    "2026-08-11": {"dsex": 5903.53106, "dsex_chg": 58.65908, "ds30": 2204.56937, "ds30_chg": 18.71622, "dses": None, "value_mn": 11154.267, "trades": 260288.0},
    "2026-08-10": {"dsex": 5844.87198, "dsex_chg": 22.55378, "ds30": 2185.85315, "ds30_chg": 8.56398, "dses": None, "value_mn": 9084.031, "trades": 225976.0},
    "2026-08-09": {"dsex": 5822.3182, "dsex_chg": -38.63965, "ds30": 2177.28917, "ds30_chg": -14.55757, "dses": None, "value_mn": 9645.915, "trades": 245942.0},
    "2026-08-06": {"dsex": 5860.95785, "dsex_chg": -33.16917, "ds30": 2191.84674, "ds30_chg": -9.90232, "dses": None, "value_mn": 11477.414, "trades": 274233.0},
    "2026-08-04": {"dsex": 5894.12702, "dsex_chg": 8.43503, "ds30": 2201.74906, "ds30_chg": -2.4215, "dses": None, "value_mn": 11114.929, "trades": 268395.0},
    "2026-08-03": {"dsex": 5885.69199, "dsex_chg": -10.09159, "ds30": 2204.17056, "ds30_chg": -9.1648, "dses": None, "value_mn": 12105.195, "trades": 275288.0},
    "2026-08-02": {"dsex": 5895.78358, "dsex_chg": 0.20058, "ds30": 2213.33536, "ds30_chg": -3.88796, "dses": None, "value_mn": 12576.233, "trades": 291079.0},
    "2026-07-30": {"dsex": 5895.583, "dsex_chg": 16.72964, "ds30": 2217.22332, "ds30_chg": 6.49485, "dses": None, "value_mn": 10434.355, "trades": 247542.0},
    "2026-07-29": {"dsex": 5878.85336, "dsex_chg": -21.41242, "ds30": 2210.72847, "ds30_chg": -8.76572, "dses": None, "value_mn": 13425.17, "trades": 311860.0},
    "2026-07-28": {"dsex": 5900.26578, "dsex_chg": 60.49412, "ds30": 2219.49419, "ds30_chg": 17.93071, "dses": None, "value_mn": 12609.304, "trades": 278917.0},
    "2026-07-27": {"dsex": 5839.77166, "dsex_chg": 55.40264, "ds30": 2201.56348, "ds30_chg": 15.35484, "dses": None, "value_mn": 8720.899, "trades": 215895.0},
    "2026-07-26": {"dsex": 5784.36902, "dsex_chg": -19.9284, "ds30": 2186.20864, "ds30_chg": -6.51383, "dses": None, "value_mn": 7806.391, "trades": 227923.0},
    "2026-07-23": {"dsex": 5804.29742, "dsex_chg": -66.76717, "ds30": 2192.72247, "ds30_chg": -23.93114, "dses": None, "value_mn": 9388.808, "trades": 249005.0},
    "2026-07-22": {"dsex": 5871.06459, "dsex_chg": -27.89961, "ds30": 2216.65361, "ds30_chg": -3.34743, "dses": None, "value_mn": 12111.307, "trades": 294367.0},
    "2026-07-21": {"dsex": 5898.9642, "dsex_chg": 41.34089, "ds30": 2220.00104, "ds30_chg": 11.16199, "dses": None, "value_mn": 11293.963, "trades": 265358.0},
    "2026-07-20": {"dsex": 5857.62331, "dsex_chg": 1.90001, "ds30": 2208.83905, "ds30_chg": -1.90671, "dses": None, "value_mn": 9665.318, "trades": 254077.0},
    "2026-07-19": {"dsex": 5855.7233, "dsex_chg": -44.63292, "ds30": 2210.74576, "ds30_chg": -16.87963, "dses": None, "value_mn": 10703.95, "trades": 277180.0},
    "2026-07-16": {"dsex": 5900.35622, "dsex_chg": -25.91911, "ds30": 2227.62539, "ds30_chg": -15.27645, "dses": None, "value_mn": 11182.474, "trades": 271376.0},
    "2026-07-15": {"dsex": 5926.27533, "dsex_chg": 15.03237, "ds30": 2242.90184, "ds30_chg": 15.80163, "dses": None, "value_mn": 15159.143, "trades": 338485.0},
    "2026-07-14": {"dsex": 5911.24296, "dsex_chg": 44.69339, "ds30": 2227.10021, "ds30_chg": 24.0077, "dses": None, "value_mn": 16512.915, "trades": 356667.0},
    "2026-07-13": {"dsex": 5866.54957, "dsex_chg": 17.33126, "ds30": 2203.09251, "ds30_chg": 2.35209, "dses": None, "value_mn": 14191.548, "trades": 328447.0},
    "2026-07-12": {"dsex": 5849.21831, "dsex_chg": 45.15477, "ds30": 2200.74042, "ds30_chg": 22.97928, "dses": None, "value_mn": 16696.374, "trades": 382533.0},
    "2026-07-09": {"dsex": 5804.06354, "dsex_chg": 33.78998, "ds30": 2177.76114, "ds30_chg": 8.51819, "dses": None, "value_mn": 14284.684, "trades": 324776.0},
    "2026-07-08": {"dsex": 5770.27356, "dsex_chg": -11.00069, "ds30": 2169.24295, "ds30_chg": -12.6055, "dses": None, "value_mn": 11561.172, "trades": 278569.0},
    "2026-07-07": {"dsex": 5781.27425, "dsex_chg": -18.24069, "ds30": 2181.84845, "ds30_chg": -10.68389, "dses": None, "value_mn": 13880.363, "trades": 316993.0},
    "2026-07-06": {"dsex": 5799.51494, "dsex_chg": 12.10414, "ds30": 2192.53234, "ds30_chg": 1.20496, "dses": None, "value_mn": 14165.738, "trades": 324330.0},
    "2026-07-05": {"dsex": 5787.4108, "dsex_chg": 43.55196, "ds30": 2191.32738, "ds30_chg": 29.31485, "dses": None, "value_mn": 15300.785, "trades": 336987.0},
    "2026-07-02": {"dsex": 5743.85884, "dsex_chg": -18.97252, "ds30": 2162.01253, "ds30_chg": -16.36804, "dses": None, "value_mn": 14395.45, "trades": 327405.0},
}

# The 10 ids one DSE daily cohort always shares an (as_of, ingested_at) pair
# across — see aggregate_latest.flatten_data (the "dse" block) and
# aggregate_latest.py:217-231 in the caller context this script was built
# against.
DSE_METRIC_IDS: tuple[str, ...] = (
    "dsex", "dsex_change", "dsex_change_pct", "ds30", "dses",
    "turnover_crore", "total_trades", "advancing", "declining", "unchanged",
)

# advancing/declining/unchanged are not present on the market_summary
# archive page at all — never inserted (honest gap). dses is ALSO always
# null in the embedded archive above (never captured by this source either)
# — extending the same honesty rule to it, NOT inserted, even though the
# original build brief listed it as nominally derivable. Judgment call —
# see the module's PR notes.
INSERTABLE_IDS: tuple[str, ...] = (
    "dsex", "dsex_change", "dsex_change_pct", "ds30",
    "turnover_crore", "total_trades",
)

INSERT_SOURCE_LABEL = "dsebd_market_summary_archive"

# Read-scope window. CONTEXT text says "as_of >= 2026-07-01"; bounded above
# at 2026-08-23 (inclusive) to deliberately exclude 2026-08-24 (tonight's
# own appender territory, out of scope for this backfill — see module
# docstring). A stray, genuinely unexpected row at 2026-07-01 is NOT
# silently skipped: it would fail the ambiguous-match check below (no
# session in the embedded table starts before 2026-07-02) and abort the
# run rather than being quietly ignored.
READ_WINDOW_START = date(2026, 7, 1)
READ_WINDOW_END = date(2026, 8, 23)

DSEX_MATCH_TOLERANCE = 0.01

# --- Hard tripwire: the controller-verified expected plan ------------------
# compute_plan() derives its OWN plan from the DB + OFFICIAL_SESSIONS_RAW.
# This is what that computed plan is cross-checked against before any
# write — a mismatch aborts, full stop. Do not hand-edit these without a
# fresh controller/owner sign-off.
EXPECTED_DELETES: frozenset[tuple[str, str]] = frozenset({
    ("2026-07-13", "2026-07-13T08:01:17Z"),
    ("2026-07-15", "2026-07-15T08:01:06Z"),
    ("2026-08-06", "2026-08-05T21:15:47Z"),
    ("2026-08-13", "2026-08-12T20:56:36Z"),
})
assert len(EXPECTED_DELETES) == 4, f"expected exactly 4 deletes, got {len(EXPECTED_DELETES)}"

EXPECTED_RESTAMPS: tuple[tuple[str, str], ...] = (
    ("2026-07-16", "2026-07-15"), ("2026-07-19", "2026-07-16"), ("2026-07-20", "2026-07-19"),
    ("2026-07-21", "2026-07-20"), ("2026-07-22", "2026-07-21"), ("2026-07-23", "2026-07-22"),
    ("2026-07-26", "2026-07-23"), ("2026-07-27", "2026-07-26"), ("2026-07-28", "2026-07-27"),
    ("2026-07-29", "2026-07-28"), ("2026-07-30", "2026-07-29"), ("2026-08-02", "2026-07-30"),
    ("2026-08-03", "2026-08-02"), ("2026-08-04", "2026-08-03"), ("2026-08-05", "2026-08-04"),
    ("2026-08-09", "2026-08-06"), ("2026-08-10", "2026-08-09"), ("2026-08-11", "2026-08-10"),
    ("2026-08-16", "2026-08-13"), ("2026-08-17", "2026-08-16"), ("2026-08-18", "2026-08-17"),
    ("2026-08-19", "2026-08-18"), ("2026-08-20", "2026-08-19"),
)
assert len(EXPECTED_RESTAMPS) == 23, f"expected exactly 23 restamps, got {len(EXPECTED_RESTAMPS)}"

EXPECTED_INSERT_DATES: frozenset[str] = frozenset({"2026-07-13", "2026-08-11", "2026-08-20"})
assert len(EXPECTED_INSERT_DATES) == 3, f"expected exactly 3 inserts, got {len(EXPECTED_INSERT_DATES)}"


class PlanError(Exception):
    """Raised when the computed plan can't be trusted — ambiguous match,
    an occupied restamp target whose occupant's own fate isn't resolved
    yet, a dsex mismatch where a duplicate was assumed, or a mismatch
    against the hard-coded expected plan. Always aborts the run."""


# ============================================================================
# Pure data model + plan computation — no I/O, fully unit-testable.
# ============================================================================


@dataclass(frozen=True)
class OfficialSession:
    as_of: date
    dsex: float
    dsex_chg: float
    ds30: float
    ds30_chg: float
    dses: float | None
    value_mn: float
    trades: float


def parse_official_sessions(
    raw: dict[str, dict[str, float | None]] = OFFICIAL_SESSIONS_RAW,
) -> dict[date, OfficialSession]:
    """Pure transform: the embedded raw dict -> {date: OfficialSession}."""
    out: dict[date, OfficialSession] = {}
    for iso, row in raw.items():
        d = date.fromisoformat(iso)
        out[d] = OfficialSession(
            as_of=d,
            dsex=float(row["dsex"]),
            dsex_chg=float(row["dsex_chg"]),
            ds30=float(row["ds30"]),
            ds30_chg=float(row["ds30_chg"]),
            dses=row["dses"],
            value_mn=float(row["value_mn"]),
            trades=float(row["trades"]),
        )
    return out


@dataclass(frozen=True)
class HistoryRow:
    metric_id: str
    as_of: date
    value: float
    ingested_at: str  # raw ISO string as returned by PostgREST — an opaque
    # pinning key, never reformatted or reparsed for anything but grouping
    # and the delete/restamp filter itself.
    source: str = ""


@dataclass(frozen=True)
class Cohort:
    as_of: date
    ingested_at: str
    rows: tuple[HistoryRow, ...]

    @property
    def dsex(self) -> float | None:
        for r in self.rows:
            if r.metric_id == "dsex":
                return r.value
        return None

    @property
    def metric_ids(self) -> tuple[str, ...]:
        return tuple(r.metric_id for r in self.rows)


def group_cohorts(rows: list[HistoryRow]) -> list[Cohort]:
    """Group flat DSE HistoryRows into cohorts sharing (as_of, ingested_at).

    Sorted ascending by (as_of, ingested_at) — the order compute_plan()
    depends on.
    """
    grouped: dict[tuple[date, str], list[HistoryRow]] = {}
    for r in rows:
        grouped.setdefault((r.as_of, r.ingested_at), []).append(r)
    cohorts = [
        Cohort(as_of=as_of, ingested_at=ing, rows=tuple(rs))
        for (as_of, ing), rs in grouped.items()
    ]
    cohorts.sort(key=lambda c: (c.as_of, c.ingested_at))
    return cohorts


def match_true_session(
    cohort: Cohort,
    official: dict[date, OfficialSession],
    tolerance: float = DSEX_MATCH_TOLERANCE,
) -> date:
    """Match a cohort to its true session by comparing dsex against every
    embedded official close. Must match EXACTLY ONE session — raises
    PlanError on zero or multiple matches (never guesses)."""
    if cohort.dsex is None:
        raise PlanError(
            f"cohort as_of={cohort.as_of} ingested_at={cohort.ingested_at} has no "
            "'dsex' row — cannot match to a true session"
        )
    candidates = [
        d for d, session in official.items()
        if abs(session.dsex - cohort.dsex) <= tolerance
    ]
    if len(candidates) != 1:
        raise PlanError(
            f"cohort as_of={cohort.as_of} ingested_at={cohort.ingested_at} "
            f"dsex={cohort.dsex} matched {len(candidates)} official session(s) "
            f"(need exactly 1): {sorted(candidates)}"
        )
    return candidates[0]


@dataclass(frozen=True)
class DeleteAction:
    as_of: date
    ingested_at: str
    metric_ids: tuple[str, ...]
    duplicate_of: date  # the true session this cohort duplicates


@dataclass(frozen=True)
class RestampAction:
    old_as_of: date
    new_as_of: date
    ingested_at: str
    metric_ids: tuple[str, ...]


@dataclass(frozen=True)
class InsertAction:
    as_of: date
    values: dict[str, float]
    source: str = INSERT_SOURCE_LABEL


Action = DeleteAction | RestampAction


@dataclass(frozen=True)
class Plan:
    ordered_actions: tuple[Action, ...]  # ascending stored-as_of execution order
    insert_dates: tuple[date, ...]
    no_actions: tuple[Cohort, ...]

    @property
    def deletes(self) -> tuple[DeleteAction, ...]:
        return tuple(a for a in self.ordered_actions if isinstance(a, DeleteAction))

    @property
    def restamps(self) -> tuple[RestampAction, ...]:
        return tuple(a for a in self.ordered_actions if isinstance(a, RestampAction))


def compute_plan(
    cohorts: list[Cohort],
    official: dict[date, OfficialSession],
    tolerance: float = DSEX_MATCH_TOLERANCE,
) -> Plan:
    """Derive the delete/restamp/insert plan from live cohorts + the
    embedded official table. See the module docstring's "Plan algorithm"
    section for the full walkthrough. Raises PlanError on anything that
    can't be resolved safely — never guesses, never silently drops a
    cohort.
    """
    cohorts_sorted = sorted(cohorts, key=lambda c: (c.as_of, c.ingested_at))
    # Keyed by the COHORT's own identity (as_of, ingested_at), never by
    # as_of alone: a resumed run after a mid-sequence crash can legitimately
    # have two cohorts sharing the same as_of at once (an unexecuted
    # duplicate still sitting at its stored as_of, plus an already-restamped
    # mover that landed on that same date because the crash happened between
    # the two related actions in a DIFFERENT prefix than ordered_actions'
    # own interleaved order would produce). Keying by as_of alone would let
    # the second cohort's match silently overwrite the first's in this dict,
    # corrupting which target the FIRST cohort resolves against.
    true_session: dict[tuple[date, str], date] = {}
    for c in cohorts_sorted:
        true_session[(c.as_of, c.ingested_at)] = match_true_session(c, official, tolerance)

    # occupied_unresolved -> occupied_permanent | vacated, as each cohort's
    # OWN stored as_of is reached in ascending order.
    slot_state: dict[date, str] = {c.as_of: "occupied_unresolved" for c in cohorts_sorted}
    # The cohort that LOGICALLY sits in each slot right now -- NOT a lookup
    # by original stored as_of. A slot's occupant can itself be a mover
    # that RESTAMPED in earlier in this same ascending pass (its own
    # Cohort.as_of field is still its OLD stored value, but its dsex is
    # what's actually there now). Getting this wrong (re-deriving "who
    # occupies target" by re-scanning original cohorts for as_of==target)
    # silently compares against the WRONG cohort's dsex whenever a slot's
    # true resident arrived via an earlier restamp rather than having
    # always been correctly dated.
    slot_occupant: dict[date, Cohort] = {}

    ordered_actions: list[Action] = []
    no_actions: list[Cohort] = []

    for c in cohorts_sorted:
        true = true_session[(c.as_of, c.ingested_at)]
        if true == c.as_of:
            slot_state[c.as_of] = "occupied_permanent"
            slot_occupant[c.as_of] = c
            no_actions.append(c)
            continue

        target = true
        state = slot_state.get(target)

        if state == "occupied_permanent":
            # Target is permanently held by a same-dsex twin (guaranteed —
            # both matched official[target].dsex within tolerance). This
            # cohort is a pure duplicate.
            occupant = slot_occupant[target]
            if occupant.dsex is None or c.dsex is None or abs(occupant.dsex - c.dsex) > tolerance:
                raise PlanError(
                    f"occupied-slot integrity check failed: cohort as_of={c.as_of} "
                    f"wants to land on {target}, which is occupied by a cohort "
                    f"(as_of={occupant.as_of}) with a DIFFERENT dsex "
                    f"({occupant.dsex} vs {c.dsex}) — that occupant is not a "
                    "verified duplicate. Aborting rather than guessing."
                )
            ordered_actions.append(
                DeleteAction(
                    as_of=c.as_of, ingested_at=c.ingested_at,
                    metric_ids=c.metric_ids, duplicate_of=target,
                )
            )
            slot_state[c.as_of] = "vacated"
        elif state in ("vacated", None):
            ordered_actions.append(
                RestampAction(
                    old_as_of=c.as_of, new_as_of=target,
                    ingested_at=c.ingested_at, metric_ids=c.metric_ids,
                )
            )
            slot_state[target] = "occupied_permanent"
            slot_occupant[target] = c
            slot_state[c.as_of] = "vacated"
        else:
            # state == "occupied_unresolved": the target's own fate hasn't
            # been decided yet by this ascending pass — only reachable by a
            # forward shift or an ordering bug, never by the real data's
            # backward-shift pattern. Abort rather than guess an order.
            raise PlanError(
                f"restamp target not yet resolved: cohort as_of={c.as_of} wants to "
                f"move to {target}, but {target}'s own stored cohort has not been "
                "resolved yet in this ascending pass (state="
                f"{state!r}). This means the shift isn't the expected "
                "backward-only pattern — abort for a human to look."
            )

    insert_dates = tuple(
        sorted(d for d in official if slot_state.get(d) != "occupied_permanent")
    )

    return Plan(
        ordered_actions=tuple(ordered_actions),
        insert_dates=insert_dates,
        no_actions=tuple(no_actions),
    )


def cross_check_plan(plan: Plan) -> None:
    """Hard tripwire: every action the computed plan wants to perform must
    be a MEMBER of the controller-verified EXPECTED_DELETES/RESTAMPS/
    INSERT_DATES superset. An action NOT in that superset (an "extra") means
    the live DB holds something the hand-verification never saw — abort,
    a human needs to look, never auto-correct.

    Fewer actions than the FULL expected set is deliberately NOT an error.
    That is the normal shape of two legitimate scenarios this script must
    support (see the module's "Usage" + idempotency notes):
      * an idempotent re-run after a fully successful --write — the
        healed table needs ZERO actions, and this must return success
        (exit 0), not abort;
      * a re-run after a mid-sequence crash — whatever an earlier partial
        --write already committed no longer needs an action here, so the
        computed plan is a genuine SUBSET of the full incident plan.
    A compute_plan() regression that silently DROPS a legitimate action
    (rather than the DB genuinely no longer needing it) is not this
    function's job to catch — that is what TestComputePlanReproducesRealIncident
    asserts by comparing compute_plan()'s direct output to EXPECTED_* on the
    real incident fixture, and what verify_post_write catches at runtime (a
    skipped session's dsex simply won't match after the write).

    Restamps additionally must preserve EXPECTED_RESTAMPS's own relative
    (ascending stored-as_of / backward-shift) order — a computed restamp
    sequence that's out of that order relative to the expected chain is the
    same ordering anomaly compute_plan's own docstring says the real
    (backward-only) data never produces, so it still aborts.
    """
    expected_deletes_norm = {(d, _normalize_iso(ia)) for d, ia in EXPECTED_DELETES}
    computed_deletes_norm = {(a.as_of.isoformat(), _normalize_iso(a.ingested_at)) for a in plan.deletes}
    extra_deletes = computed_deletes_norm - expected_deletes_norm
    if extra_deletes:
        raise PlanError(
            f"DELETE plan mismatch — unexpected extra delete(s) not in the "
            f"verified plan: {sorted(extra_deletes)}"
        )

    computed_restamps = tuple(
        (a.old_as_of.isoformat(), a.new_as_of.isoformat()) for a in plan.restamps
    )
    expected_restamp_set = set(EXPECTED_RESTAMPS)
    extra_restamps = [r for r in computed_restamps if r not in expected_restamp_set]
    if extra_restamps:
        raise PlanError(
            f"RESTAMP plan mismatch — unexpected extra restamp(s) not in the "
            f"verified plan: {sorted(extra_restamps)}"
        )
    expected_order_filtered = tuple(r for r in EXPECTED_RESTAMPS if r in set(computed_restamps))
    if computed_restamps != expected_order_filtered:
        raise PlanError(
            "RESTAMP plan mismatch — computed restamps are out of the "
            f"verified backward-shift order: computed={computed_restamps} "
            f"expected_subsequence={expected_order_filtered}"
        )

    computed_inserts = {d.isoformat() for d in plan.insert_dates}
    extra_inserts = computed_inserts - EXPECTED_INSERT_DATES
    if extra_inserts:
        raise PlanError(
            f"INSERT plan mismatch — unexpected extra insert(s) not in the "
            f"verified plan: {sorted(extra_inserts)}"
        )


def _normalize_iso(s: str) -> datetime:
    """'...Z' and '...+00:00' both parse to the same aware datetime —
    PostgREST returns the latter, the hand-verified table above uses the
    former; compare by value, never by raw string."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- Insert-row derivation ---------------------------------------------------


def _official_prior_close(official: dict[date, OfficialSession], d: date) -> float:
    """The dsex close of the session immediately BEFORE ``d`` in the
    embedded table's own trading-day order (not the calendar day before —
    weekends/holidays aren't trading days)."""
    ordered = sorted(official)
    idx = ordered.index(d)
    if idx == 0:
        raise PlanError(f"no prior session in the embedded table before {d}")
    return official[ordered[idx - 1]].dsex


def build_insert_rows(
    insert_dates: tuple[date, ...],
    official: dict[date, OfficialSession],
) -> list[InsertAction]:
    """Derive INSERT rows for never-captured sessions from the embedded
    official table. Only INSERTABLE_IDS are populated — advancing/
    declining/unchanged/dses are never on this archive page (honest gap,
    see the module docstring/INSERTABLE_IDS comment).

    dsex_change_pct = dsex_chg / prior_close * 100, rounded to 5 decimal
    places (matching the embedded table's own captured precision for
    dsex/dsex_chg — the live scraper, scrapers/dse_market.py, does not
    round this field at all since it reads the page's own displayed value
    directly; here we DERIVE it, so a rounding choice has to be made, and
    5dp keeps it no more precise than its own inputs).

    turnover_crore = value_mn / 10, rounded to 4 decimal places — same
    rounding precision scrapers/dse_market.py uses for its own
    ``round(turnover_crore, 4)`` (different raw input scale — Taka vs
    Taka-millions — same crore unit, same rounding convention).
    """
    out: list[InsertAction] = []
    for d in insert_dates:
        session = official[d]
        prior_close = _official_prior_close(official, d)
        pct = round(session.dsex_chg / prior_close * 100, 5)
        values = {
            "dsex": session.dsex,
            "dsex_change": session.dsex_chg,
            "dsex_change_pct": pct,
            "ds30": session.ds30,
            "turnover_crore": round(session.value_mn / 10, 4),
            "total_trades": session.trades,
        }
        out.append(InsertAction(as_of=d, values=values))
    return out


# ============================================================================
# I/O — Supabase read/write. Credentials from the environment ONLY, never
# hardcoded (AGENTS.md landmine 18 / repo-wide rule). Mirrors
# utils/supabase_reader.py + utils/supabase_writer.py's own conventions
# (apikey + Bearer headers, `params=` dict so PostgREST filter values like
# an exact ingested_at timestamptz get percent-encoded correctly — the
# SAME pattern utils.supabase_writer.verify_landed_count already uses for
# an ingested_at filter).
# ============================================================================

_DEFAULT_TIMEOUT = 30
_PAGE_SIZE = 1000


def _resolve_credentials(url: str | None, key: str | None) -> tuple[str, str]:
    """Local resolver — intentionally not imported from utils.supabase_writer
    or utils.supabase_reader, mirroring supabase_reader.py's own documented
    reasoning: each caller owns the exception type its error path needs.
    Here that's SupabaseWriteError — this script is a writer end to end."""
    resolved_url = url or os.environ.get("SUPABASE_URL")
    resolved_key = (
        key
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SERVICE_KEY")
    )
    if not resolved_url:
        raise SupabaseWriteError("SUPABASE_URL not set in env or kwargs")
    if not resolved_key:
        raise SupabaseWriteError(
            "SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SERVICE_KEY) not set in env or kwargs"
        )
    return resolved_url.rstrip("/"), resolved_key


def fetch_dse_history_rows(
    *,
    start: date = READ_WINDOW_START,
    end: date = READ_WINDOW_END,
    url: str | None = None,
    key: str | None = None,
    session: "requests.Session | None" = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> list[HistoryRow]:
    """Page through metric_history for the 10 DSE ids in [start, end]."""
    base_url, resolved_key = _resolve_credentials(url, key)
    headers = {"apikey": resolved_key, "Authorization": f"Bearer {resolved_key}"}
    sess = session or requests.Session()
    endpoint = f"{base_url}/rest/v1/metric_history"

    rows: list[HistoryRow] = []
    offset = 0
    while True:
        params = {
            "select": "metric_id,as_of,value,ingested_at,source",
            "metric_id": "in.(" + ",".join(DSE_METRIC_IDS) + ")",
            "as_of": [f"gte.{start.isoformat()}", f"lte.{end.isoformat()}"],
            "order": "as_of.asc,ingested_at.asc,metric_id.asc",
            "limit": str(_PAGE_SIZE),
            "offset": str(offset),
        }
        try:
            resp = sess.get(endpoint, params=params, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise SupabaseWriteError(f"fetch_dse_history_rows network error: {e}") from e
        if resp.status_code not in (200, 206):
            raise SupabaseWriteError(
                f"fetch_dse_history_rows returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        page = resp.json()
        for r in page:
            rows.append(HistoryRow(
                metric_id=r["metric_id"],
                as_of=date.fromisoformat(r["as_of"]),
                value=float(r["value"]),
                ingested_at=r["ingested_at"],
                source=r.get("source", ""),
            ))
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return rows


def execute_delete(
    action: DeleteAction, *, url: str | None = None, key: str | None = None,
    session: "requests.Session | None" = None, timeout: int = _DEFAULT_TIMEOUT,
) -> int:
    """DELETE the rows pinned by (metric_id in ..., as_of=eq, ingested_at=eq).
    Raises SupabaseWriteError if the affected row count doesn't match the
    cohort's own row count."""
    base_url, key_ = _resolve_credentials(url, key)
    headers = {
        "apikey": key_, "Authorization": f"Bearer {key_}",
        "Content-Type": "application/json", "Prefer": "return=representation",
    }
    params = {
        "metric_id": "in.(" + ",".join(action.metric_ids) + ")",
        "as_of": f"eq.{action.as_of.isoformat()}",
        "ingested_at": f"eq.{action.ingested_at}",
    }
    sess = session or requests.Session()
    endpoint = f"{base_url}/rest/v1/metric_history"
    try:
        resp = sess.delete(endpoint, params=params, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(f"DELETE as_of={action.as_of} network error: {e}") from e
    if resp.status_code not in (200, 204):
        raise SupabaseWriteError(
            f"DELETE as_of={action.as_of} HTTP {resp.status_code}: {resp.text[:200]}"
        )
    deleted = len(resp.json()) if resp.text else 0
    if deleted != len(action.metric_ids):
        raise SupabaseWriteError(
            f"DELETE as_of={action.as_of}: expected {len(action.metric_ids)} rows, "
            f"deleted {deleted} — aborting, DB state may have drifted since planning."
        )
    return deleted


def execute_restamp(
    action: RestampAction, *, url: str | None = None, key: str | None = None,
    session: "requests.Session | None" = None, timeout: int = _DEFAULT_TIMEOUT,
) -> int:
    """PATCH as_of: old -> new for the cohort's rows. A live re-check GET
    right before the PATCH implements the "occupied at execution time"
    tripwire from the module docstring: the target must be genuinely empty
    at this exact moment, or we abort rather than let Postgres's own PK
    violation surface as an opaque error."""
    base_url, key_ = _resolve_credentials(url, key)
    headers_read = {"apikey": key_, "Authorization": f"Bearer {key_}"}
    sess = session or requests.Session()
    endpoint = f"{base_url}/rest/v1/metric_history"

    check_params = {
        "select": "metric_id",
        "metric_id": "eq.dsex",
        "as_of": f"eq.{action.new_as_of.isoformat()}",
    }
    try:
        check_resp = sess.get(endpoint, params=check_params, headers=headers_read, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(f"pre-restamp occupancy check network error: {e}") from e
    if check_resp.status_code not in (200, 206):
        raise SupabaseWriteError(
            f"pre-restamp occupancy check HTTP {check_resp.status_code}: {check_resp.text[:200]}"
        )
    if check_resp.json():
        raise SupabaseWriteError(
            f"RESTAMP {action.old_as_of} -> {action.new_as_of} aborted: target slot is "
            "occupied at execution time (expected empty — an earlier delete/restamp in "
            "this SAME run should have vacated it). DB state may have drifted since "
            "planning; re-run with a fresh plan rather than forcing this write."
        )

    headers_write = {
        "apikey": key_, "Authorization": f"Bearer {key_}",
        "Content-Type": "application/json", "Prefer": "return=representation",
    }
    params = {
        "metric_id": "in.(" + ",".join(action.metric_ids) + ")",
        "as_of": f"eq.{action.old_as_of.isoformat()}",
        "ingested_at": f"eq.{action.ingested_at}",
    }
    payload = {"as_of": action.new_as_of.isoformat()}
    try:
        resp = sess.patch(endpoint, params=params, json=payload, headers=headers_write, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(
            f"RESTAMP {action.old_as_of} -> {action.new_as_of} network error: {e}"
        ) from e
    if resp.status_code not in (200, 204):
        raise SupabaseWriteError(
            f"RESTAMP {action.old_as_of} -> {action.new_as_of} HTTP {resp.status_code}: "
            f"{resp.text[:200]}"
        )
    updated = len(resp.json()) if resp.text else 0
    if updated != len(action.metric_ids):
        raise SupabaseWriteError(
            f"RESTAMP {action.old_as_of} -> {action.new_as_of}: expected "
            f"{len(action.metric_ids)} rows, updated {updated} — aborting."
        )
    return updated


def execute_insert(
    action: InsertAction, *, url: str | None = None, key: str | None = None,
    session: "requests.Session | None" = None, timeout: int = _DEFAULT_TIMEOUT,
) -> int:
    """INSERT (via the standard upsert path — on_conflict is a no-op here
    since the target slot is verified empty by the plan) one never-captured
    session's derivable ids."""
    write_ts = datetime.now(timezone.utc)
    return upsert_metric_history(
        data=action.values,
        as_of=action.as_of,
        source=action.source,
        ingested_at=write_ts,
        url=url,
        service_key=key,
        timeout=timeout,
        session=session,
    )


# --- Post-write verification --------------------------------------------


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    problems: tuple[str, ...] = field(default_factory=tuple)


def verify_post_write(
    official: dict[date, OfficialSession],
    deleted_pairs: frozenset[tuple[str, str]],
    *,
    url: str | None = None,
    key: str | None = None,
    session: "requests.Session | None" = None,
) -> VerificationResult:
    """Re-read the dsex series for the full window and assert:
      1. every embedded official session matches the stored dsex within
         DSEX_MATCH_TOLERANCE,
      2. no duplicate as_of dates remain,
      3. the deleted cohorts are actually gone.
    """
    problems: list[str] = []
    rows = fetch_dse_history_rows(start=READ_WINDOW_START, end=READ_WINDOW_END, url=url, key=key, session=session)
    dsex_rows = [r for r in rows if r.metric_id == "dsex"]

    by_as_of: dict[date, list[HistoryRow]] = {}
    for r in dsex_rows:
        by_as_of.setdefault(r.as_of, []).append(r)

    for d, session_official in sorted(official.items()):
        found = by_as_of.get(d)
        if not found:
            problems.append(f"{d}: no dsex row found (expected {session_official.dsex})")
            continue
        if len(found) > 1:
            problems.append(f"{d}: {len(found)} duplicate dsex rows found")
        if abs(found[0].value - session_official.dsex) > DSEX_MATCH_TOLERANCE:
            problems.append(
                f"{d}: stored dsex {found[0].value} != official {session_official.dsex}"
            )

    for as_of_iso, ingested_at in deleted_pairs:
        d = date.fromisoformat(as_of_iso)
        still_there = [r for r in dsex_rows if r.as_of == d and _normalize_iso(r.ingested_at) == _normalize_iso(ingested_at)]
        if still_there:
            problems.append(f"deleted cohort as_of={as_of_iso} ingested_at={ingested_at} still present")

    return VerificationResult(ok=not problems, problems=tuple(problems))


# ============================================================================
# CLI
# ============================================================================


def _print_plan(plan: Plan) -> None:
    print(f"\n=== Computed plan ({len(plan.deletes)} deletes, {len(plan.restamps)} restamps, "
          f"{len(plan.insert_dates)} inserts, {len(plan.no_actions)} no-ops) ===")
    for a in plan.deletes:
        print(f"  DELETE   as_of={a.as_of} ingested_at={a.ingested_at} "
              f"({len(a.metric_ids)} rows, duplicate of {a.duplicate_of})")
    for a in plan.restamps:
        print(f"  RESTAMP  {a.old_as_of} -> {a.new_as_of}  (ingested_at={a.ingested_at}, "
              f"{len(a.metric_ids)} rows)")
    for d in plan.insert_dates:
        print(f"  INSERT   as_of={d}")


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument("--dry-run", action="store_true",
                    help="compute + print the plan; NO Supabase writes (default)")
    p.add_argument("--write", action="store_true",
                    help="perform the REAL Supabase writes. Needs SUPABASE_URL + "
                         "SUPABASE_SERVICE_ROLE_KEY in the environment. Owner-run only.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    args.dry_run = not args.write  # --write is the only flag that changes behavior

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    official = parse_official_sessions()
    rows = fetch_dse_history_rows()
    cohorts = group_cohorts(rows)
    print(f"Read {len(rows)} row(s) across {len(cohorts)} cohort(s) in "
          f"{READ_WINDOW_START}..{READ_WINDOW_END}.")

    try:
        plan = compute_plan(cohorts, official)
        cross_check_plan(plan)
    except PlanError as e:
        logger.error("plan aborted: %s", e)
        print(f"\nABORT: {e}")
        return 1

    _print_plan(plan)

    if not plan.deletes and not plan.restamps and not plan.insert_dates:
        print("\nNo actions needed — DB already matches the official table "
              "(idempotent re-run).")

    if args.dry_run:
        print("\nDRY RUN (default) — nothing written to Supabase. Pass --write "
              "(with SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY set) to execute.")
        return 0

    deleted_pairs: set[tuple[str, str]] = set()
    for action in plan.ordered_actions:
        if isinstance(action, DeleteAction):
            n = execute_delete(action)
            print(f"  deleted {n} row(s) at as_of={action.as_of}")
            deleted_pairs.add((action.as_of.isoformat(), action.ingested_at))
        else:
            n = execute_restamp(action)
            print(f"  restamped {n} row(s): {action.old_as_of} -> {action.new_as_of}")

    insert_actions = build_insert_rows(plan.insert_dates, official)
    for action in insert_actions:
        n = execute_insert(action)
        print(f"  inserted {n} row(s) at as_of={action.as_of}")

    print("\n=== Post-write verification ===")
    result = verify_post_write(official, frozenset(deleted_pairs))
    if result.ok:
        print(f"PASS — {len(official)} sessions verified, no duplicates, "
              f"{len(deleted_pairs)} deleted cohort(s) confirmed gone.")
        return 0
    print("FAIL —")
    for problem in result.problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(run())
