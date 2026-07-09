"""Freshness assessment + the four historical-freeze retro-tests (E2.1).

The retro-tests are the load-bearing part: the sentinel exists to have caught
the DSE, pink-sheet, CRAR, and 06-05 freezes. Synthetic rows mirror each
cluster's real max(as_of); the test pins the sentinel's verdict at a chosen
``today`` so a future refactor can't silently stop firing on them.
"""
from __future__ import annotations

from datetime import date

from sentinel.cadence import load_cadence_map
from sentinel.freshness import assess, is_breach

# --- is_breach: per-cadence grace boundaries --------------------------------

def test_daily_breach_tolerates_weekend_gap():
    # Sat 2026-07-04; last close Thu 2026-07-02 → within 2 trading days → fresh.
    assert is_breach(date(2026, 7, 2), "daily", date(2026, 7, 4)) is False


def test_daily_breach_fires_beyond_two_trading_days():
    # DSE frozen at 2026-06-11, checked 2026-07-04 → far beyond 2 sessions.
    assert is_breach(date(2026, 6, 11), "daily", date(2026, 7, 4)) is True


def test_daily_breach_boundary_two_trading_days():
    # From Sat 2026-07-04 the two allowed sessions back are Thu 07-02 and
    # Wed 07-01 (Fri/Sat are non-trading). 07-01 is the floor (allowed);
    # Tue 06-30 is a third session back → breach.
    assert is_breach(date(2026, 7, 1), "daily", date(2026, 7, 4)) is False
    assert is_breach(date(2026, 6, 30), "daily", date(2026, 7, 4)) is True


def test_monthly_breach_boundary_at_45_days():
    today = date(2026, 7, 20)
    assert is_breach(date(2026, 6, 5), "monthly", today) is False   # exactly 45d
    assert is_breach(date(2026, 6, 4), "monthly", today) is True    # 46d


def test_quarterly_breach_boundary_at_165_days():
    today = date(2026, 3, 15)
    assert is_breach(today - _days(165), "quarterly", today) is False
    assert is_breach(today - _days(166), "quarterly", today) is True


def test_fiscal_year_breach_boundary_at_400_days():
    today = date(2026, 7, 4)
    assert is_breach(today - _days(400), "fiscal_year", today) is False
    assert is_breach(today - _days(401), "fiscal_year", today) is True


def test_unknown_cadence_never_breaches_here():
    assert is_breach(date(2020, 1, 1), "century", date(2026, 7, 4)) is False


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


# --- assess: retro-tests against the four freeze clusters --------------------

def _row(metric_id, as_of, ingested_at=None):
    return {"metric_id": metric_id, "as_of": as_of, "ingested_at": ingested_at or f"{as_of}T00:00:00+00:00"}


def test_retro_dse_cluster_breaches():
    """DSE index frozen at 2026-06-11 (E1.2) — daily cadence, must breach."""
    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("dsex", "2026-06-11"), _row("dse_close_GP", "2026-06-10")],
        rows_monthly=[],
        cadence_map=m,
        today=date(2026, 7, 4),
    )
    ids = {b.metric_id for b in report.breaches}
    assert "dsex" in ids
    assert "dse_close_GP" in ids


def test_retro_pink_sheet_cluster_breaches():
    """Pink sheet frozen at 2025-12-31 (E1.5) — monthly cadence, must breach."""
    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("lng_price_usd_mmbtu", "2025-12-31"),
                    _row("wheat_price_usd_mt", "2025-12-31")],
        rows_monthly=[],
        cadence_map=m,
        today=date(2026, 7, 4),
    )
    ids = {b.metric_id for b in report.breaches}
    assert {"lng_price_usd_mmbtu", "wheat_price_usd_mt"} <= ids


def test_retro_crar_cluster_breaches_at_quarterly_grace():
    """banking_sector_crar last vintage 2025-09-30 — quarterly; 277d > 165d grace."""
    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("banking_sector_crar", "2025-09-30")],
        rows_monthly=[],
        cadence_map=m,
        today=date(2026, 7, 4),
    )
    assert {b.metric_id for b in report.breaches} == {"banking_sector_crar"}


def test_retro_0605_cluster_is_cadence_correct():
    """The 06-05 monthly cluster is FRESH in early July, STALE only past 45d grace.

    This is the subtlety the whole vintage design turns on: money_multiplier is
    MONTHLY, so a 2026-06-05 vintage is not a same-day emergency — it is within
    grace until ~2026-07-20 and only then a breach.
    """
    m = load_cadence_map()
    rows = [_row("money_multiplier", "2026-06-05"),
            _row("general_inflation", "2026-06-05")]

    early = assess(rows_daily=rows, rows_monthly=[], cadence_map=m, today=date(2026, 7, 4))
    assert {f.metric_id for f in early.fresh} == {"money_multiplier", "general_inflation"}
    assert early.breaches == []

    late = assess(rows_daily=rows, rows_monthly=[], cadence_map=m, today=date(2026, 7, 25))
    assert {b.metric_id for b in late.breaches} == {"money_multiplier", "general_inflation"}


# --- assess: cross-table + edge cases ---------------------------------------

def test_future_as_of_is_excluded_from_latest():
    """debt_gdp_ratio carries a 2031 IMF projection — must not read as fresh-from-future."""
    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("debt_gdp_ratio", "2031-12-31"),
                    _row("debt_gdp_ratio", "2025-12-31")],
        rows_monthly=[],
        cadence_map=m,
        today=date(2026, 2, 1),
    )
    fresh = {f.metric_id: f for f in report.fresh}
    assert "debt_gdp_ratio" in fresh
    assert fresh["debt_gdp_ratio"].latest_as_of == date(2025, 12, 31)


def test_metric_only_in_monthly_table_resolves_monthly():
    m = load_cadence_map()
    report = assess(
        rows_daily=[],
        rows_monthly=[_row("cpi_headline_monthly", "2026-06-01")],
        cadence_map=m,
        today=date(2026, 6, 20),
    )
    assert {f.metric_id for f in report.fresh} == {"cpi_headline_monthly"}


def test_unmapped_metric_is_surfaced_not_skipped():
    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("totally_unknown_xyz", "2020-01-01")],
        rows_monthly=[],
        cadence_map=m,
        today=date(2026, 7, 4),
    )
    assert {u.metric_id for u in report.unmapped} == {"totally_unknown_xyz"}
    assert report.breaches == []


def test_metric_with_only_future_rows_is_unmapped():
    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("gdp", "2099-12-31")],
        rows_monthly=[],
        cadence_map=m,
        today=date(2026, 7, 4),
    )
    # no non-future vintage to judge → can't score → unmapped
    assert {u.metric_id for u in report.unmapped} == {"gdp"}


def test_max_across_both_tables_wins():
    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("private_sector_credit_yoy_pct", "2026-05-01")],
        rows_monthly=[_row("private_sector_credit_yoy_pct", "2026-06-01")],
        cadence_map=m,
        today=date(2026, 6, 25),
    )
    entry = {f.metric_id: f for f in report.fresh}["private_sector_credit_yoy_pct"]
    assert entry.latest_as_of == date(2026, 6, 1)
    assert set(entry.tables) == {"metric_history", "metric_history_monthly"}
