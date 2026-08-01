from datetime import date, timedelta

from briefing.freshness import _is_stale, assess_freshness

TODAY = date(2026, 5, 30)
CADENCE = {"call_money_rate": "daily", "tbond_5y_yield": "weekly",
           "policy_rate_repo": "monthly", "some_fiscal": "monthly"}
CORE = {"call_money_rate", "tbond_5y_yield", "policy_rate_repo"}


def test_all_fresh_passes():
    latest = {"call_money_rate": date(2026, 5, 29), "tbond_5y_yield": date(2026, 5, 26),
              "policy_rate_repo": date(2026, 5, 1), "some_fiscal": date(2026, 5, 10)}
    r = assess_freshness(latest, CADENCE, CORE, TODAY, aggregate_ok_recent=True)
    assert r.core_stale is False
    assert r.stale_series == []


def test_stale_core_daily_metric_trips_gate():
    latest = {"call_money_rate": date(2026, 5, 20),  # 10d old, daily window=2d -> stale
              "tbond_5y_yield": date(2026, 5, 26), "policy_rate_repo": date(2026, 5, 1),
              "some_fiscal": date(2026, 5, 10)}
    r = assess_freshness(latest, CADENCE, CORE, TODAY, aggregate_ok_recent=True)
    assert r.core_stale is True


def test_no_recent_aggregate_trips_gate_even_if_as_of_fresh():
    latest = {"call_money_rate": date(2026, 5, 29), "tbond_5y_yield": date(2026, 5, 26),
              "policy_rate_repo": date(2026, 5, 1), "some_fiscal": date(2026, 5, 10)}
    r = assess_freshness(latest, CADENCE, CORE, TODAY, aggregate_ok_recent=False)
    assert r.core_stale is True
    assert any("aggregate" in reason for reason in r.reasons)


def test_stale_peripheral_only_yields_banner_not_skip():
    latest = {"call_money_rate": date(2026, 5, 29), "tbond_5y_yield": date(2026, 5, 26),
              "policy_rate_repo": date(2026, 5, 1),
              "some_fiscal": date(2026, 1, 1)}  # ancient, monthly window=45d -> stale, but peripheral
    r = assess_freshness(latest, CADENCE, CORE, TODAY, aggregate_ok_recent=True)
    assert r.core_stale is False
    assert r.stale_series == ["some_fiscal"]


def test_data_as_of_is_min_core_as_of():
    latest = {"call_money_rate": date(2026, 5, 29), "tbond_5y_yield": date(2026, 5, 26),
              "policy_rate_repo": date(2026, 5, 1), "some_fiscal": date(2026, 5, 10)}
    r = assess_freshness(latest, CADENCE, CORE, TODAY, aggregate_ok_recent=True)
    assert r.data_as_of == date(2026, 5, 1)  # oldest core reading


def test_absent_core_metric_trips_gate():
    # policy_rate_repo is core but entirely missing from history -> must skip
    latest = {"call_money_rate": date(2026, 5, 29), "tbond_5y_yield": date(2026, 5, 26)}
    r = assess_freshness(latest, CADENCE, CORE, TODAY, aggregate_ok_recent=True)
    assert r.core_stale is True
    assert any("absent" in reason for reason in r.reasons)


# --- boundary tests: pin the exact _STALE_DAYS_BY_CADENCE values -------------
#
# Each test asserts BOTH sides of its window's boundary, so it goes RED if
# that specific cadence's value is ever reverted (review round 1, item 2) —
# not just moved in the wrong direction generally. Uses `_is_stale` directly
# (not `assess_freshness`) to isolate the window lookup from core/peripheral
# routing, aggregate-recency, and absent-metric logic already covered above.

_BOUNDARY_TODAY = date(2026, 8, 10)


def test_quarterly_boundary_165_fresh_166_stale():
    assert _is_stale(_BOUNDARY_TODAY - timedelta(days=165), "quarterly", _BOUNDARY_TODAY) is False
    assert _is_stale(_BOUNDARY_TODAY - timedelta(days=166), "quarterly", _BOUNDARY_TODAY) is True


def test_daily_boundary_2_fresh_3_stale():
    assert _is_stale(_BOUNDARY_TODAY - timedelta(days=2), "daily", _BOUNDARY_TODAY) is False
    assert _is_stale(_BOUNDARY_TODAY - timedelta(days=3), "daily", _BOUNDARY_TODAY) is True


def test_monthly_boundary_60_fresh_61_stale():
    assert _is_stale(_BOUNDARY_TODAY - timedelta(days=60), "monthly", _BOUNDARY_TODAY) is False
    assert _is_stale(_BOUNDARY_TODAY - timedelta(days=61), "monthly", _BOUNDARY_TODAY) is True
