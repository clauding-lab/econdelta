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
    """The nine explicit ids this PR was asked to add -- a regression guard
    against silently dropping one during a future refactor."""
    assert set(WATCHLIST_IDS) == {
        "gross_reserves_usd_bn",
        "private_sector_credit_yoy_pct",
        "monthly_export",
        "monthly_import",
        "monthly_remittance",
        "nbr_fytd_collected_cr",
        "general_inflation",
        "food_inflation",
        "non_food_inflation",
    }


# ── predicate (a): value frozen while as_of advances ─────────────────────────

def test_predicate_a_fires_when_value_stuck_but_as_of_keeps_advancing(state_path):
    """A monthly reserves figure that never actually moves while as_of stamps
    a NEW month each time is the as_of-forgery signature."""
    # Baseline run establishes the "prior" (nothing to compare against yet,
    # so it can never itself count as a repeat) -- exactly
    # _MIN_CONSECUTIVE_FROZEN_ADVANCES more runs are then needed to trip it.
    # Three runs total, 3 days apart (inside the re-alert quiet window), so
    # this only crosses the threshold ONCE.
    n = _Notifier()
    day = DAY0
    as_of = date(2026, 1, 31)
    breached = []
    for i in range(1 + _MIN_CONSECUTIVE_FROZEN_ADVANCES):
        breached = _run(
            {"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": as_of},
            day=day, path=state_path, notifier=n,
        )
        day += timedelta(days=3)
        as_of += timedelta(days=30)

    assert len(breached) == 1
    assert breached[0].predicate == "value_frozen_as_of_advanced"
    assert len(n.calls) == 1


def test_predicate_a_requires_consecutive_repeats_not_a_single_coincidence(state_path):
    """A single repeat (the SECOND run, first real comparison) must not
    alert on its own -- only sustained repetition looks like forgery."""
    n = _Notifier()
    breached = _run(
        {"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": date(2026, 1, 31)},
        day=DAY0, path=state_path, notifier=n,
    )
    assert breached == []  # baseline run -- nothing to compare against yet

    breached = _run(
        {"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": date(2026, 2, 28)},
        day=DAY0 + timedelta(days=3), path=state_path, notifier=n,
    )
    assert breached == []  # 1 repeat observed -- below the threshold of 2

    breached = _run(
        {"gross_reserves_usd_bn": 30.0}, {"gross_reserves_usd_bn": date(2026, 3, 30)},
        day=DAY0 + timedelta(days=6), path=state_path, notifier=n,
    )
    # 2 consecutive repeats -- _MIN_CONSECUTIVE_FROZEN_ADVANCES trips it now.
    assert _MIN_CONSECUTIVE_FROZEN_ADVANCES == 2
    assert len(breached) == 1
    assert n.calls[0]["fields"]["count"] == "1"


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
