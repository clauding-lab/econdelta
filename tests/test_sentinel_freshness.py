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


# --- assess: accepted-stale (source-lag) metrics never alert -----------------

def test_accepted_stale_metrics_route_to_accepted_stale_not_breaches():
    """tax_gdp_ratio (WB, stuck at 2021) and rev_gdp_ratio (IMF, latest 2024) lag
    by DESIGN. Both have a real fiscal_year cadence + vintage, so absent a carve-out
    they'd breach the 400d grace and fire an unactionable DAILY alert. They must
    route to the silent ``accepted_stale`` bucket, never ``breaches``/``fresh``."""
    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("tax_gdp_ratio", "2021-12-31"),   # ~1653d old on today below
                    _row("rev_gdp_ratio", "2024-12-31")],   # ~557d old (> 400d grace)
        rows_monthly=[],
        cadence_map=m,
        today=date(2026, 7, 11),
    )
    assert {s.metric_id for s in report.accepted_stale} == {"tax_gdp_ratio", "rev_gdp_ratio"}
    assert report.breaches == []
    assert report.fresh == []
    # The vintage is still reported honestly (not hidden, just not alerted).
    tax = {s.metric_id: s for s in report.accepted_stale}["tax_gdp_ratio"]
    assert tax.latest_as_of == date(2021, 12, 31)
    assert tax.breach is False


def test_should_send_stays_silent_on_accepted_stale_only_non_heartbeat():
    """The whole point: a day with only source-lag metrics stale → the sentinel
    says NOTHING on a non-heartbeat day (no daily alert-fatigue nag)."""
    from sentinel.report import should_send

    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("tax_gdp_ratio", "2021-12-31")],
        rows_monthly=[],
        cadence_map=m,
        today=date(2026, 7, 11),
    )
    assert report.breaches == []
    assert should_send(report, is_heartbeat_day=False) is False   # silent, no nag
    assert should_send(report, is_heartbeat_day=True) is True     # weekly health ping still fires


# --- assess: retired ids (fell out of the source's universe) -----------------

def test_retired_dse_tickers_route_to_accepted_stale_not_breaches():
    """dse_close_KOHINOOR/LINDEBD/UNIQUEHRL fell out of the DS30 constituents
    at the ~2026-07-16 rebalance; backfill_dse_dayend.py has no delisting
    handling so they simply stopped being written and now breach daily grace.
    Distinct reason from source-lag (ACCEPTED_STALE_METRIC_IDS) but same
    silent treatment — must never fire the daily breach alert for a ticker
    that will never be written again."""
    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("dse_close_KOHINOOR", "2026-07-15"),
                    _row("dse_close_LINDEBD", "2026-07-15"),
                    _row("dse_close_UNIQUEHRL", "2026-07-15")],
        rows_monthly=[],
        cadence_map=m,
        today=date(2026, 8, 1),
    )
    ids = {"dse_close_KOHINOOR", "dse_close_LINDEBD", "dse_close_UNIQUEHRL"}
    assert {s.metric_id for s in report.accepted_stale} == ids
    assert report.breaches == []
    retired = {s.metric_id: s for s in report.accepted_stale}["dse_close_KOHINOOR"]
    assert retired.latest_as_of == date(2026, 7, 15)
    assert retired.breach is False


def test_other_dse_close_tickers_still_breach_normally():
    """A different, still-tracked dse_close_* ticker freezing for a genuinely
    dead scraper is NOT in RETIRED_METRIC_IDS and must still breach —
    retirement routing is scoped to the three named ids only."""
    m = load_cadence_map()
    report = assess(
        rows_daily=[_row("dse_close_GP", "2026-06-10")],
        rows_monthly=[],
        cadence_map=m,
        today=date(2026, 8, 1),
    )
    assert {b.metric_id for b in report.breaches} == {"dse_close_GP"}
    assert report.accepted_stale == []


# --- assess: 2026-08-08 frozen-charts triage (landmine 50) -------------------


def test_cpi_12m_food_and_nonfood_route_to_accepted_stale():
    """cpi_12m_food_monthly/cpi_12m_nonfood_monthly have no live source
    anywhere post-seed-death (unlike the headline cpi_12m_avg_monthly, which
    the live appender derives from general_inflation) — must never fire the
    daily/monthly breach alert."""
    from sentinel.freshness import ACCEPTED_STALE_METRIC_IDS

    assert {"cpi_12m_food_monthly", "cpi_12m_nonfood_monthly"} <= ACCEPTED_STALE_METRIC_IDS

    m = load_cadence_map()
    report = assess(
        rows_daily=[],
        rows_monthly=[_row("cpi_12m_food_monthly", "2026-03-01"),
                      _row("cpi_12m_nonfood_monthly", "2026-03-01")],
        cadence_map=m,
        today=date(2026, 8, 8),
    )
    ids = {"cpi_12m_food_monthly", "cpi_12m_nonfood_monthly"}
    assert {s.metric_id for s in report.accepted_stale} >= ids
    assert not (ids & {b.metric_id for b in report.breaches})


def test_imports_usd_mn_monthly_routes_to_accepted_stale():
    """BB publishes cif imports ~2 months late; no standalone monthly figure
    exists beyond May 2026 — structural source lag, not a pipeline fault."""
    from sentinel.freshness import ACCEPTED_STALE_METRIC_IDS

    assert "imports_usd_mn_monthly" in ACCEPTED_STALE_METRIC_IDS

    m = load_cadence_map()
    report = assess(
        rows_daily=[], rows_monthly=[_row("imports_usd_mn_monthly", "2026-05-01")],
        cadence_map=m, today=date(2026, 8, 8),
    )
    assert "imports_usd_mn_monthly" in {s.metric_id for s in report.accepted_stale}
    assert "imports_usd_mn_monthly" not in {b.metric_id for b in report.breaches}


def test_exports_usd_mn_monthly_routes_to_accepted_stale():
    """Backfilled to Jun 2026 from EPB press; the EPB portal itself is
    JS-rendered/unscrapeable — no live writer, accepted-stale pending
    ongoing source research."""
    from sentinel.freshness import ACCEPTED_STALE_METRIC_IDS

    assert "exports_usd_mn_monthly" in ACCEPTED_STALE_METRIC_IDS

    m = load_cadence_map()
    report = assess(
        rows_daily=[], rows_monthly=[_row("exports_usd_mn_monthly", "2026-06-01")],
        cadence_map=m, today=date(2026, 8, 8),
    )
    assert "exports_usd_mn_monthly" in {s.metric_id for s in report.accepted_stale}
    assert "exports_usd_mn_monthly" not in {b.metric_id for b in report.breaches}


def test_chart_feeding_metric_ids_has_exactly_16_ids():
    from sentinel.freshness import CHART_FEEDING_METRIC_IDS

    assert len(CHART_FEEDING_METRIC_IDS) == 16
    assert CHART_FEEDING_METRIC_IDS == {
        "remittance_usd_mn_monthly", "exports_usd_mn_monthly", "imports_usd_mn_monthly",
        "cpi_12m_avg_monthly", "cpi_p2p_food_monthly", "cpi_p2p_nonfood_monthly",
        "tbill_91d_yield_monthly", "tbill_182d_yield_monthly", "tbill_364d_yield_monthly",
        "yield_2y_monthly", "yield_5y_monthly", "yield_10y_monthly",
        "yield_15y_monthly", "yield_20y_monthly",
        "gross_reserves_usd_bn_monthly", "net_reserves_bpm6_usd_bn_monthly",
    }
