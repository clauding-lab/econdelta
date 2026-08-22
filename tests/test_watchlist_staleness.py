"""Tests for the watchlist staleness gate (utils/staleness.check_watchlist_staleness).

Three failure-shape predicates, all as_of/ingest-aware (unlike the plain
value-equality alarm in check_value_staleness):
  (a) value frozen while as_of advances
  (b) as_of frozen while ingested_at advances (approximated as "present in
      `data` this run" -- see the module docstring in utils/staleness.py)
  (c) no ingest at all for N days
"""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from utils.staleness import (
    _MIN_CONSECUTIVE_FROZEN_ADVANCES,
    WATCHLIST_IDS,
    _WatchlistBudget,
    check_watchlist_staleness,
)

DAY0 = date(2026, 5, 1)


class _Notifier:
    def __init__(self):
        self.calls = []

    def __call__(self, level, title, message, fields=None):
        self.calls.append({"level": level, "title": title, "message": message,
                           "fields": fields or {}})
        return True


@pytest.fixture
def state_path(tmp_path) -> Path:
    return tmp_path / "watchlist_staleness_state.json"


_TEST_WATCHLIST = {"gross_reserves_usd_bn": _WatchlistBudget(as_of_frozen_days=60, no_ingest_days=10)}


def _run(data, source_as_of, *, day, path, notifier, watchlist=None):
    return check_watchlist_staleness(
        data, source_as_of, today=day, state_path=path, notifier=notifier,
        watchlist=watchlist or _TEST_WATCHLIST,
    )


# ── real coverage gap this watchlist closes ──────────────────────────────────

def test_watchlist_ids_match_the_task_spec():
    """The nine explicit ids the original PR was asked to add, plus
    m2_growth_yoy_pct (PR-C, Opus review round 1, M8: private_sector_
    credit_yoy_pct's new sibling on the same monthly HTML-page source
    family) -- a regression guard against silently dropping one during a
    future refactor."""
    assert set(WATCHLIST_IDS) == {
        "gross_reserves_usd_bn",
        "private_sector_credit_yoy_pct",
        "m2_growth_yoy_pct",
        "monthly_export",
        "monthly_import",
        "monthly_remittance",
        "nbr_fytd_collected_cr",
        "general_inflation",
        "food_inflation",
        "non_food_inflation",
    }


# ── predicate (a): value frozen while as_of advances ─────────────────────────
#
# These ids are MONTHLY: in production, aggregate_latest runs DAILY, so
# as_of only actually advances on roughly 1 run in 30 -- the other ~29 are
# "same as_of as yesterday" runs. A prior version of predicate (a) reset its
# consecutive-repeat counter to 0 on every one of those ~29 no-advance runs
# (not just genuine value-changed ones), so at production cadence it could
# NEVER reach the 2-in-a-row threshold: simulated over 365 daily runs against
# a monthly as_of, it fired zero times. The tests below run at that same
# daily cadence rather than "one call per as_of change" (which never
# exercised the no-advance runs and so never could have caught the bug).


def _daily_cadence_run(*, days: int, value_at_month, path, notifier):
    """Simulate `days` consecutive DAILY calls where as_of only advances
    once every 30 days (this watchlist's real monthly cadence). Returns the
    breach list from the run on which as_of FIRST produced a breach, or []
    if none did across the whole window.

    `value_at_month(month_index)` supplies the value for that as_of period,
    so a caller can hold it frozen (the forgery case) or advance it (the
    healthy case) across month boundaries.
    """
    first_breach: list = []
    for i in range(days):
        day = DAY0 + timedelta(days=i)
        month_index = i // 30
        as_of = date(2026, 1, 31) + timedelta(days=30 * month_index)
        value = value_at_month(month_index)
        breached = _run(
            {"gross_reserves_usd_bn": value}, {"gross_reserves_usd_bn": as_of},
            day=day, path=path, notifier=notifier,
        )
        if breached and not first_breach:
            first_breach = breached
    return first_breach


def test_predicate_a_fires_on_the_second_as_of_advance_at_production_cadence(state_path):
    """The reviewer's exact repro, fixed: 90 DAILY runs, as_of advancing
    every 30 of them, value frozen throughout. Must stay silent through the
    1st advance (only one comparison exists so far) and fire on the 2nd."""
    n = _Notifier()
    breached = _daily_cadence_run(
        days=90, value_at_month=lambda _m: 30.0, path=state_path, notifier=n,
    )
    assert _MIN_CONSECUTIVE_FROZEN_ADVANCES == 2
    assert len(breached) == 1
    assert breached[0].predicate == "value_frozen_as_of_advanced"
    assert breached[0].indicator_id == "gross_reserves_usd_bn"
    assert n.calls  # actually notified, not just returned


def test_predicate_a_silent_through_one_as_of_advance_at_production_cadence(state_path):
    """Isolates the boundary the fix depends on: after exactly ONE as_of
    advance (30 daily runs, all frozen), there is only one comparison on
    record -- not yet the 2 consecutive repeats predicate (a) requires."""
    n = _Notifier()
    breached = []
    for i in range(31):  # day 0 (baseline) .. day 30 (the 1st advance)
        day = DAY0 + timedelta(days=i)
        as_of = date(2026, 1, 31) if i < 30 else date(2026, 3, 2)
        breached = _run(
            {"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": as_of},
            day=day, path=state_path, notifier=n,
        )
    assert breached == []
    assert n.calls == []


def test_predicate_a_never_fires_when_value_moves_at_production_cadence(state_path):
    """The healthy case at the SAME daily cadence: as_of advances monthly
    and the value genuinely moves each time -- must never alert across a
    full simulated year."""
    n = _Notifier()
    breached = _daily_cadence_run(
        days=365, value_at_month=lambda m: 30.0 + m, path=state_path, notifier=n,
    )
    assert breached == []
    assert n.calls == []


def test_predicate_a_does_not_fire_when_value_genuinely_moves(state_path):
    """The healthy case: as_of advances AND the value moves too. Must never
    alert -- this is what a real monthly reserves print looks like."""
    n = _Notifier()
    day, as_of, value = DAY0, date(2026, 1, 31), 30.0
    for i in range(6):
        breached = _run(
            {"gross_reserves_usd_bn": value}, {"gross_reserves_usd_bn": as_of},
            day=day, path=state_path, notifier=n,
        )
        assert breached == []
        day += timedelta(days=30)
        as_of += timedelta(days=30)
        value += 0.5
    assert n.calls == []


# ── predicate (b): as_of frozen while the pipeline keeps ingesting ──────────

def test_predicate_b_fires_when_as_of_stuck_far_past_its_publish_cycle(state_path):
    """A dead source edition (landmine 34's class): the pipeline ingests
    every day but the reporting period itself never advances."""
    n = _Notifier()
    budget = _TEST_WATCHLIST["gross_reserves_usd_bn"].as_of_frozen_days
    as_of = date(2026, 1, 31)

    for offset in range(budget):
        breached = _run(
            {"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": as_of},
            day=DAY0 + timedelta(days=offset), path=state_path, notifier=n,
        )
    assert breached == []  # one day short of budget

    breached = _run(
        {"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": as_of},
        day=DAY0 + timedelta(days=budget), path=state_path, notifier=n,
    )
    assert len(breached) == 1
    assert breached[0].predicate == "as_of_frozen"


def test_predicate_b_does_not_fire_within_one_publish_cycle(state_path):
    """A single normal month of the same as_of (source hasn't republished
    yet) must not alert -- that is the everyday healthy state."""
    n = _Notifier()
    as_of = date(2026, 1, 31)
    breached = []
    for offset in range(25):  # well under the 60d budget
        breached = _run(
            {"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": as_of},
            day=DAY0 + timedelta(days=offset), path=state_path, notifier=n,
        )
    assert breached == []
    assert n.calls == []


def test_no_as_of_this_run_skips_predicates_a_and_b_without_crashing(state_path):
    """An id present in `data` but with no recovered as_of this run (parser
    date-recovery gap) must not be judged on staleness it can't see."""
    n = _Notifier()
    breached = _run({"gross_reserves_usd_bn": 30.0}, {}, day=DAY0,
                    path=state_path, notifier=n)
    assert breached == []
    breached = _run({"gross_reserves_usd_bn": 30.0}, {}, day=DAY0 + timedelta(days=200),
                    path=state_path, notifier=n)
    assert breached == []


# ── predicate (c): no ingest at all ──────────────────────────────────────────

def test_predicate_c_fires_after_sustained_absence(state_path):
    """The id vanishes from `data` entirely -- a stronger signal than
    'present but unchanged'."""
    n = _Notifier()
    budget = _TEST_WATCHLIST["gross_reserves_usd_bn"].no_ingest_days
    _run({"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": date(2026, 1, 31)},
         day=DAY0, path=state_path, notifier=n)

    breached = []
    for offset in range(1, budget):
        breached = _run({}, {}, day=DAY0 + timedelta(days=offset),
                        path=state_path, notifier=n)
    assert breached == []  # still inside the no-ingest grace

    breached = _run({}, {}, day=DAY0 + timedelta(days=budget),
                    path=state_path, notifier=n)
    assert len(breached) == 1
    assert breached[0].predicate == "no_ingest"


def test_predicate_c_never_fires_for_an_id_never_once_seen(state_path):
    """No baseline, no verdict -- mirrors check_value_staleness's warm-up
    discipline instead of guessing from a cold start."""
    n = _Notifier()
    breached = []
    for offset in range(50):
        breached = _run({}, {}, day=DAY0 + timedelta(days=offset),
                        path=state_path, notifier=n)
    assert breached == []
    assert json.loads(state_path.read_text())["metrics"] == {}


def test_missing_run_does_not_reset_state_and_reappearance_resumes_correctly(state_path):
    """The exact "counter reset" bug fixed in check_value_staleness, applied
    here too: one missing run must not erase what was known before it."""
    n = _Notifier()
    _run({"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": date(2026, 1, 31)},
         day=DAY0, path=state_path, notifier=n)

    # Missing for a few runs, then reappears with the exact same value+as_of.
    for offset in range(1, 4):
        _run({}, {}, day=DAY0 + timedelta(days=offset), path=state_path, notifier=n)

    breached = _run(
        {"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": date(2026, 1, 31)},
        day=DAY0 + timedelta(days=4), path=state_path, notifier=n,
    )
    # Same value AND same as_of on reappearance -- neither predicate (a) (as_of
    # didn't advance) nor (b) (nowhere near the 60d budget) should fire.
    assert breached == []
    saved = json.loads(state_path.read_text())["metrics"]["gross_reserves_usd_bn"]
    assert saved["value"] == 30.0
    assert saved["as_of"] == "2026-01-31"


# ── re-alert throttle ────────────────────────────────────────────────────────

def test_realert_is_throttled_like_the_value_staleness_alarm(state_path):
    """A persistent breach must not notify every single day forever."""
    n = _Notifier()
    budget = _TEST_WATCHLIST["gross_reserves_usd_bn"].no_ingest_days
    _run({"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": date(2026, 1, 31)},
         day=DAY0, path=state_path, notifier=n)

    for offset in range(budget, budget + 6):
        _run({}, {}, day=DAY0 + timedelta(days=offset), path=state_path, notifier=n)

    # Exactly one alert across the whole quiet-period stretch, not one per day.
    assert len(n.calls) == 1
