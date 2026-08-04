"""Tests for the stillness alarm (utils/staleness.py).

The alarm's whole value is that it stays quiet on healthy data and speaks up on
a freeze. Both halves are load-bearing: an alarm that cries wolf gets muted, and
a muted alarm is the state we were already in. So the false-positive cases here
matter as much as the detection ones.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from utils.staleness import (
    _MIN_RUNS_BEFORE_ALERTING,
    _REALERT_AFTER_DAYS,
    _STANDING_VALUE_IDS,
    _UNCHANGED_BUDGET_DAYS,
    check_value_staleness,
)

DAY0 = date(2026, 5, 1)


class _Notifier:
    """Capture notify() calls instead of posting to Discord."""

    def __init__(self):
        self.calls = []

    def __call__(self, level, title, message, fields=None):
        self.calls.append({"level": level, "title": title, "message": message,
                           "fields": fields or {}})
        return True


def _registry(*specs):
    """specs: (id, cadence) pairs."""
    return [{"id": i, "cadence": c} for i, c in specs]


def _run(data, registry, *, day, path, notifier):
    return check_value_staleness(
        data, registry, today=day, state_path=path, notifier=notifier
    )


def _drive(data, registry, *, days, path, notifier, start=DAY0):
    """Run the checker once per day over `days` consecutive days, same data."""
    out = []
    for offset in range(days):
        out = _run(data, registry, day=start + timedelta(days=offset),
                   path=path, notifier=notifier)
    return out


@pytest.fixture
def state_path(tmp_path) -> Path:
    return tmp_path / "staleness_state.json"


# ── the alarm fires on a real freeze ─────────────────────────────────────────

def test_frozen_daily_metric_alerts_once_past_its_budget(state_path):
    """The measured food-price case: one daily value, never changing. The alarm
    must stay silent through the budget window and then speak exactly once."""
    n = _Notifier()
    reg = _registry(("food_rice_coarse", "daily"))
    budget = _UNCHANGED_BUDGET_DAYS["daily"]

    # one day short of the budget — still silent
    breached = _drive({"food_rice_coarse": 49.0}, reg,
                      days=budget, path=state_path, notifier=n)
    assert breached == []
    assert n.calls == []

    # the day it crosses
    breached = _run({"food_rice_coarse": 49.0}, reg,
                    day=DAY0 + timedelta(days=budget), path=state_path, notifier=n)
    assert len(breached) == 1
    assert breached[0].indicator_id == "food_rice_coarse"
    assert breached[0].days_unchanged == budget
    assert len(n.calls) == 1
    assert n.calls[0]["level"] == "warning"
    assert "food_rice_coarse" in n.calls[0]["message"]


def test_repeat_alerts_are_suppressed_then_resume(state_path):
    """A frozen source stays frozen for weeks. Re-reporting every morning is how
    an alert channel becomes background noise, so re-alerting is rate-limited."""
    n = _Notifier()
    reg = _registry(("food_rice_coarse", "daily"))
    budget = _UNCHANGED_BUDGET_DAYS["daily"]
    first = DAY0 + timedelta(days=budget)

    _drive({"food_rice_coarse": 49.0}, reg, days=budget + 1,
           path=state_path, notifier=n)
    assert len(n.calls) == 1

    # quiet period — still breaching, still reported to the caller, no new alert
    for offset in range(1, _REALERT_AFTER_DAYS):
        breached = _run({"food_rice_coarse": 49.0}, reg,
                        day=first + timedelta(days=offset),
                        path=state_path, notifier=n)
        assert len(breached) == 1
        assert len(n.calls) == 1, f"re-alerted on day {offset} of the quiet period"

    # and speaks again once the quiet period expires
    _run({"food_rice_coarse": 49.0}, reg,
         day=first + timedelta(days=_REALERT_AFTER_DAYS),
         path=state_path, notifier=n)
    assert len(n.calls) == 2


def test_one_batched_alert_for_many_frozen_metrics(state_path):
    """All eight food prices froze together off one source page. Eight separate
    Discord posts every morning is the failure mode this guards."""
    n = _Notifier()
    ids = [f"food_{i}" for i in range(8)]
    reg = _registry(*[(i, "daily") for i in ids])
    data = {i: 10.0 for i in ids}

    _drive(data, reg, days=_UNCHANGED_BUDGET_DAYS["daily"] + 1,
           path=state_path, notifier=n)

    assert len(n.calls) == 1
    for i in ids:
        assert i in n.calls[0]["message"]
    assert n.calls[0]["fields"]["count"] == "8"


# ── the alarm stays quiet on healthy data ────────────────────────────────────

def test_a_moving_value_never_alerts(state_path):
    """The everyday case. If this ever fires the alarm is worthless."""
    n = _Notifier()
    reg = _registry(("usd_bdt_exchange_rate", "daily"))
    for offset in range(120):
        breached = _run({"usd_bdt_exchange_rate": 122.0 + offset * 0.01}, reg,
                        day=DAY0 + timedelta(days=offset),
                        path=state_path, notifier=n)
        assert breached == []
    assert n.calls == []


def test_a_change_resets_the_counter(state_path):
    """Self-healing: once a source starts publishing again the clock restarts,
    with no manual acknowledgement step to forget about."""
    n = _Notifier()
    reg = _registry(("food_rice_coarse", "daily"))
    budget = _UNCHANGED_BUDGET_DAYS["daily"]

    _drive({"food_rice_coarse": 49.0}, reg, days=budget,
           path=state_path, notifier=n)
    # source publishes a new price on the day it would otherwise have breached
    breached = _run({"food_rice_coarse": 51.0}, reg,
                    day=DAY0 + timedelta(days=budget),
                    path=state_path, notifier=n)
    assert breached == []
    assert n.calls == []
    saved = json.loads(state_path.read_text())["metrics"]["food_rice_coarse"]
    assert saved["unchanged_since"] == (DAY0 + timedelta(days=budget)).isoformat()
    assert saved["runs"] == 1


def test_quarterly_repeats_are_normal_and_do_not_alert(state_path):
    """A quarterly indicator is re-stamped daily at the same value for ~90 days
    BY DESIGN. Budgets have to clear the publication period or the alarm fires
    on every healthy quarterly metric, every quarter."""
    n = _Notifier()
    reg = _registry(("gross_npl_ratio", "quarterly"))
    breached = _drive({"gross_npl_ratio": 32.26}, reg, days=91,
                      path=state_path, notifier=n)
    assert breached == []
    assert n.calls == []


def test_standing_rates_are_exempt(state_path):
    """The policy corridor held 10.00% for six years legitimately. Flatness here
    carries no signal, so it is excluded rather than budgeted — see the module
    docstring for where that failure IS detected instead."""
    n = _Notifier()
    reg = _registry(*[(i, "monthly") for i in sorted(_STANDING_VALUE_IDS)])
    breached = _drive({i: 10.0 for i in _STANDING_VALUE_IDS}, reg, days=400,
                      path=state_path, notifier=n)
    assert breached == []
    assert n.calls == []
    assert json.loads(state_path.read_text())["metrics"] == {}


def test_non_numeric_and_missing_values_are_skipped(state_path):
    """Absent data is the fetch layer's problem. A None that never changes must
    not read as a frozen value, or every unimplemented indicator alerts."""
    n = _Notifier()
    reg = _registry(("a", "daily"), ("b", "daily"), ("c", "daily"), ("d", "daily"))
    data = {"a": None, "b": "n/a", "c": {"nested": 1}}   # "d" absent entirely
    breached = _drive(data, reg, days=200, path=state_path, notifier=n)
    assert breached == []
    assert n.calls == []


def test_booleans_are_not_treated_as_numbers(state_path):
    """bool subclasses int, so True would compare equal to 1.0 forever."""
    n = _Notifier()
    reg = _registry(("flag", "daily"))
    breached = _drive({"flag": True}, reg, days=200, path=state_path, notifier=n)
    assert breached == []


def test_unknown_cadence_is_not_budgeted(state_path):
    """An indicator with a cadence we have no budget for is tracked but never
    alerted — inventing a budget for an unrecognised cadence would be guessing."""
    n = _Notifier()
    reg = _registry(("odd", "biannual"))
    breached = _drive({"odd": 1.0}, reg, days=500, path=state_path, notifier=n)
    assert breached == []
    assert n.calls == []
    assert "odd" in json.loads(state_path.read_text())["metrics"]


# ── state file robustness ────────────────────────────────────────────────────

def test_corrupt_state_restarts_the_tracker_without_raising(state_path):
    """aggregate runs unattended under systemd. A malformed cache must cost the
    alarm its warm-up, never take down the run."""
    n = _Notifier()
    state_path.write_text("{not json at all")
    reg = _registry(("x", "daily"))
    breached = _run({"x": 1.0}, reg, day=DAY0, path=state_path, notifier=n)
    assert breached == []
    assert json.loads(state_path.read_text())["metrics"]["x"]["runs"] == 1


def test_state_with_a_future_unchanged_since_is_rejected(state_path):
    """metric_history currently holds an as_of of 2031-12-31 from a mis-parse.
    A future date yields a negative age, which would make a metric permanently
    un-alertable — the quietest possible failure."""
    n = _Notifier()
    state_path.write_text(json.dumps({"metrics": {
        "x": {"value": 1.0, "unchanged_since": "2031-12-31", "runs": 99}}}))
    reg = _registry(("x", "daily"))
    _run({"x": 1.0}, reg, day=DAY0, path=state_path, notifier=n)
    saved = json.loads(state_path.read_text())["metrics"]["x"]
    assert saved["unchanged_since"] == DAY0.isoformat()
    assert saved["runs"] == 1


def test_retired_ids_are_pruned_from_state(state_path):
    """State is rebuilt from the registry each run, so a renamed or deleted
    indicator drops out instead of accumulating forever."""
    n = _Notifier()
    _run({"old": 1.0, "new": 2.0}, _registry(("old", "daily"), ("new", "daily")),
         day=DAY0, path=state_path, notifier=n)
    assert set(json.loads(state_path.read_text())["metrics"]) == {"old", "new"}

    _run({"new": 2.0}, _registry(("new", "daily")),
         day=DAY0 + timedelta(days=1), path=state_path, notifier=n)
    assert set(json.loads(state_path.read_text())["metrics"]) == {"new"}


def test_warm_up_requires_repeated_observation(state_path):
    """A hand-seeded or restored state file must not let one run's worth of
    evidence trigger an alert — the counter has to have been observed."""
    n = _Notifier()
    seeded = (DAY0 - timedelta(days=365)).isoformat()
    state_path.write_text(json.dumps({"metrics": {
        "x": {"value": 1.0, "unchanged_since": seeded, "runs": 1}}}))
    reg = _registry(("x", "daily"))

    breached = _run({"x": 1.0}, reg, day=DAY0, path=state_path, notifier=n)
    assert breached == []          # runs=2, below the minimum
    assert n.calls == []

    for offset in range(1, _MIN_RUNS_BEFORE_ALERTING):
        breached = _run({"x": 1.0}, reg, day=DAY0 + timedelta(days=offset),
                        path=state_path, notifier=n)
    assert len(breached) == 1       # minimum met, and the year-long age counts
    assert breached[0].days_unchanged > 365


def test_alert_body_names_the_source_of_truth(state_path):
    """The alert has to tell a reader what to DO. Naming the value, the date it
    stopped moving and the budget is what turns it into a source to go check."""
    n = _Notifier()
    reg = _registry(("food_rice_coarse", "daily"))
    _drive({"food_rice_coarse": 49.0}, reg,
           days=_UNCHANGED_BUDGET_DAYS["daily"] + 1, path=state_path, notifier=n)

    body = n.calls[0]["message"]
    assert "49.0" in body
    assert DAY0.isoformat() in body
    assert "budget 14d" in body
    assert n.calls[0]["fields"]["worst"].startswith("food_rice_coarse")
