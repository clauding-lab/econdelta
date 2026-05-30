from datetime import date

from briefing.freshness import assess_freshness

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
    latest = {"call_money_rate": date(2026, 5, 20),  # 10d old, daily window=1d -> stale
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
              "some_fiscal": date(2026, 1, 1)}  # ancient, monthly window=35d -> stale, but peripheral
    r = assess_freshness(latest, CADENCE, CORE, TODAY, aggregate_ok_recent=True)
    assert r.core_stale is False
    assert r.stale_series == ["some_fiscal"]


def test_data_as_of_is_min_core_as_of():
    latest = {"call_money_rate": date(2026, 5, 29), "tbond_5y_yield": date(2026, 5, 26),
              "policy_rate_repo": date(2026, 5, 1), "some_fiscal": date(2026, 5, 10)}
    r = assess_freshness(latest, CADENCE, CORE, TODAY, aggregate_ok_recent=True)
    assert r.data_as_of == date(2026, 5, 1)  # oldest core reading
