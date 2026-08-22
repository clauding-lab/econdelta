from datetime import date, timedelta

from briefing import config
from briefing.freshness import assess_freshness


def test_loaders_cover_known_core_ids():
    indicators = config.load_indicators()
    thr = config.thresholds_by_metric(indicators)
    cad = config.cadence_by_metric(indicators)
    # call_money_rate is a real daily money-market indicator with a threshold
    assert "call_money_rate" in thr
    assert thr["call_money_rate"] is not None
    assert cad["call_money_rate"] in {"daily", "weekly", "monthly", "quarterly", "fiscal_year"}


def test_core_ids_are_subset_of_tracked():
    indicators = config.load_indicators()
    tracked = config.tracked_metric_ids(indicators)
    assert config.CORE_METRIC_IDS <= set(tracked)


def test_tbond_5y_10y_cadence_is_monthly_not_weekly():
    """Opus review round 1, C1 (blocker): tbond_5y_yield/tbond_10y_yield now
    carry REAL auction dates (aggregate_latest._derive_daily_yields_from_
    auctions), not run-date stamps. BGTB 5y/10y auction roughly monthly-to-
    quarterly, so a "weekly" cadence (briefing/freshness.py's 8-day window)
    would read core_stale on almost every week's briefing. Bills genuinely
    DO auction roughly weekly and must stay on the tighter window."""
    indicators = config.load_indicators()
    cadence = config.cadence_by_metric(indicators)
    assert cadence["tbond_5y_yield"] == "monthly"
    assert cadence["tbond_10y_yield"] == "monthly"
    assert cadence["bill_bond_rates"] == "weekly"
    assert cadence["tbill_182d_yield"] == "weekly"
    assert cadence["tbill_364d_yield"] == "weekly"


def test_assess_freshness_survives_a_30_day_old_5y_10y_auction_date():
    """Regression pin for C1: a 30-day-old 5y/10y auction date (typical for
    a bond auctioning roughly monthly) must NOT trip the briefing's
    core_stale gate. Every OTHER core id is held fresh so this isolates the
    5y/10y cadence fix specifically."""
    indicators = config.load_indicators()
    cadence = config.cadence_by_metric(indicators)
    today = date(2026, 8, 23)
    fresh = today - timedelta(days=1)
    stale_ish = today - timedelta(days=30)
    latest = {mid: fresh for mid in config.CORE_METRIC_IDS}
    latest["tbond_5y_yield"] = stale_ish
    latest["tbond_10y_yield"] = stale_ish

    r = assess_freshness(latest, cadence, config.CORE_METRIC_IDS, today, aggregate_ok_recent=True)
    assert r.core_stale is False, r.reasons
