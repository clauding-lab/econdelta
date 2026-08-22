"""Digest formatting + send-gating for the freshness sentinel (E2.1)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sentinel.freshness import FreshnessReport, MetricFreshness
from sentinel.report import HEARTBEAT_WEEKDAY, format_digest, should_send


def _breach(mid, cadence="daily", age=30):
    return MetricFreshness(
        metric_id=mid, cadence=cadence, latest_as_of=date(2026, 6, 1),
        latest_ingested_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        age_days=age, breach=True, tables=("metric_history",),
    )


def _fresh(mid):
    return MetricFreshness(
        metric_id=mid, cadence="daily", latest_as_of=date(2026, 7, 3),
        latest_ingested_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        age_days=1, breach=False, tables=("metric_history",),
    )


def test_should_send_on_any_breach():
    report = FreshnessReport(breaches=[_breach("dsex")])
    assert should_send(report, is_heartbeat_day=False) is True


def test_should_stay_silent_when_fresh_and_not_heartbeat():
    report = FreshnessReport(fresh=[_fresh("dsex")])
    assert should_send(report, is_heartbeat_day=False) is False


def test_should_send_heartbeat_when_fresh_on_heartbeat_day():
    report = FreshnessReport(fresh=[_fresh("dsex")])
    assert should_send(report, is_heartbeat_day=True) is True


def test_breach_digest_is_a_warning_listing_each_metric():
    report = FreshnessReport(
        breaches=[_breach("dsex", "daily", 23), _breach("lng_price_usd_mmbtu", "monthly", 185)],
        fresh=[_fresh("usd_bdt_mid")],
    )
    level, title, message, fields = format_digest(report)
    assert level == "warning"
    assert "2 stale" in title
    assert "dsex" in message
    assert "lng_price_usd_mmbtu" in message
    assert fields["Breached"] == "2"
    assert fields["Fresh"] == "1"


def test_heartbeat_digest_is_info_all_fresh():
    report = FreshnessReport(fresh=[_fresh("a"), _fresh("b")])
    level, title, message, _fields = format_digest(report)
    assert level == "info"
    assert "all 2 fresh" in title


def test_heartbeat_names_unmapped_dedupe_candidates():
    unmapped = MetricFreshness(
        metric_id="dse_dsex_close", cadence=None, latest_as_of=date(2026, 6, 1),
        latest_ingested_at=None, age_days=33, breach=False, tables=("metric_history",),
    )
    report = FreshnessReport(fresh=[_fresh("a")], unmapped=[unmapped])
    _level, _title, message, fields = format_digest(report)
    assert "dse_dsex_close" in message
    assert fields["Unmapped"] == "1"


def test_breach_digest_caps_via_character_budget_not_a_fixed_line_count():
    """The "Other" tier's cap is now character-budget-aware (fits as many
    lines as the remaining Discord ceiling allows), not a fixed line count --
    enough long-id breaches must still overflow into a hidden-count tail."""
    long_ids = [f"some_fairly_long_internal_metric_identifier_number_{i}" for i in range(60)]
    report = FreshnessReport(breaches=[_breach(mid, "daily", 100 - i) for i, mid in enumerate(long_ids)])
    _level, _title, message, fields = format_digest(report)
    assert "hidden" in message and " more (" in message
    assert fields["Breached"] == "60"
    assert len(message) <= 1900


def test_heartbeat_weekday_is_sunday():
    assert HEARTBEAT_WEEKDAY == 6


# --- 2026-08-08 chart-feeding grouping (landmine 50) -------------------------


def test_chart_feeding_breaches_are_listed_first_under_their_own_heading():
    """The exact incident this reorders the digest to prevent: a
    chart-feeding freeze (a reader-visible chart) must never sit buried
    below lower-stakes internal-parity breaches."""
    report = FreshnessReport(
        breaches=[
            _breach("internal_parity_metric", "quarterly", 200),
            _breach("remittance_usd_mn_monthly", "monthly", 60),
        ],
    )
    _level, _title, message, _fields = format_digest(report)
    chart_idx = message.index("CHART-FEEDING")
    remit_idx = message.index("remittance_usd_mn_monthly")
    other_idx = message.index("internal_parity_metric")
    assert chart_idx < remit_idx < other_idx


def test_chart_feeding_group_is_worst_first_within_itself():
    # freshness.assess sorts report.breaches worst-first BEFORE format_digest
    # ever sees it -- construct the input already in that order (oldest/
    # worst first) the way assess() would, and assert format_digest
    # PRESERVES it within the chart-feeding group (does not re-sort or
    # reverse it).
    report = FreshnessReport(
        breaches=[
            _breach("remittance_usd_mn_monthly", "monthly", 90),   # worst
            _breach("gross_reserves_usd_bn_monthly", "monthly", 50),
        ],
    )
    _level, _title, message, _fields = format_digest(report)
    assert message.index("remittance_usd_mn_monthly") < message.index("gross_reserves_usd_bn_monthly")


def test_no_chart_feeding_breaches_omits_the_heading_entirely():
    """Degrades to the EXACT prior single-list digest shape when nothing
    chart-feeding is stale — no structural change for the common case."""
    report = FreshnessReport(breaches=[_breach("some_internal_metric", "daily", 10)])
    _level, _title, message, _fields = format_digest(report)
    assert "CHART-FEEDING" not in message
    assert "Other:" not in message


def test_chart_feeding_breaches_are_never_truncated_even_when_other_is_large():
    """The core fix: chart-feeding breaches get their OWN slice of the
    digest, never competing with "Other" for a shared line-count budget --
    all 8 chart-feeding lines must appear regardless of how many "Other"
    breaches also exist."""
    import sentinel.report as report_mod

    real_chart_ids = list(report_mod.CHART_FEEDING_METRIC_IDS)[:8]
    chart = [_breach(mid, "monthly", 100 - i) for i, mid in enumerate(real_chart_ids)]
    other = [_breach(f"internal_{i}", "daily", 50 - i) for i in range(30)]
    report = FreshnessReport(breaches=chart + other)
    total = len(chart) + len(other)

    _level, _title, message, fields = format_digest(report)
    for mid in real_chart_ids:
        assert f"`{mid}`" in message
    assert fields["Breached"] == str(total)
    assert len(message) <= 2000


# --- L1 regression: a full chart-feeding group must never leave a dangling
# --- "Other:" heading with zero lines under it (2026-08-08 Opus review) ----


def test_no_other_breaches_omits_the_other_heading_entirely():
    """L1's original spirit under the new per-tier budgets: a heading must
    never appear with zero lines under it. Chart-feeding present, "Other"
    genuinely empty -- no "Other (...)" heading should print at all."""
    chart_25 = [_breach(f"chart_feeding_synthetic_{i}", "monthly", 200 - i) for i in range(25)]

    import sentinel.report as report_mod

    original = report_mod.CHART_FEEDING_METRIC_IDS
    try:
        report_mod.CHART_FEEDING_METRIC_IDS = frozenset(m.metric_id for m in chart_25)
        report = FreshnessReport(breaches=chart_25)
        _level, _title, message, _fields = format_digest(report)
    finally:
        report_mod.CHART_FEEDING_METRIC_IDS = original

    assert "Other (" not in message
    for m in chart_25:
        assert f"`{m.metric_id}`" in message


def test_other_tier_overflow_names_the_tier_it_was_trimmed_from():
    """HIGH-3 (2026-08-22 round-1 review): the hidden-count tail line names
    WHICH TIER was trimmed ('N other'), not a bare count and not the
    internal-category grouping an earlier version used."""
    other = [
        _breach(f"npl_rate_sector_{i}_x_extra_padding_to_force_overflow", "quarterly", 200)
        for i in range(80)
    ]
    report = FreshnessReport(breaches=other)
    _level, _title, message, _fields = format_digest(report)
    assert "more (" in message and "other) hidden" in message
    assert len(message) <= 1900


def test_reader_visible_tiers_are_trimmed_only_as_a_last_resort():
    """The reviewer's exact scenario shape: 78 reader-visible breaches (split
    across chart-feeding and brief-surfaced) plus 40 internal-only breaches.
    Other must be trimmed first and completely before either reader-visible
    tier loses a single line; the top (worst) chart-feeding lines must
    survive even if the tier ultimately has to give something up."""
    import sentinel.report as report_mod

    chart_ids = [f"chart_feeding_synth_{i}_padding_for_length" for i in range(20)]
    brief_ids = [f"brief_surfaced_synth_{i}_padding_for_length" for i in range(58)]
    original_chart = report_mod.CHART_FEEDING_METRIC_IDS
    original_brief = report_mod.BRIEF_SURFACED_METRIC_IDS
    try:
        report_mod.CHART_FEEDING_METRIC_IDS = frozenset(chart_ids)
        report_mod.BRIEF_SURFACED_METRIC_IDS = frozenset(brief_ids)

        chart = [_breach(mid, "monthly", 300 - i) for i, mid in enumerate(chart_ids)]
        brief = [_breach(mid, "daily", 200 - i) for i, mid in enumerate(brief_ids)]
        other = [_breach(f"internal_parity_{i}_padding_for_length", "daily", 30 - i)
                 for i in range(40)]
        report = FreshnessReport(breaches=chart + brief + other)

        _level, _title, message, fields = format_digest(report)
    finally:
        report_mod.CHART_FEEDING_METRIC_IDS = original_chart
        report_mod.BRIEF_SURFACED_METRIC_IDS = original_brief

    assert len(message) <= 1900
    assert fields["Breached"] == str(len(chart) + len(brief) + len(other))
    # The worst (first, lowest age_days) chart-feeding line must survive --
    # trimming drops from the END of each tier, so the front is the last
    # thing to go.
    assert f"`{chart_ids[0]}`" in message


# --- M5: accepted-stale ids that are ALSO chart-feeding are permanently ----
# --- invisible without a weekly heartbeat note (2026-08-08 Opus review) ----


def _accepted_stale(mid, as_of=date(2026, 6, 1)):
    return MetricFreshness(
        metric_id=mid, cadence="monthly", latest_as_of=as_of,
        latest_ingested_at=datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc),
        age_days=90, breach=False, tables=("metric_history_monthly",),
    )


def test_m5_heartbeat_names_parked_chart_feeding_accepted_stale_ids():
    report = FreshnessReport(
        fresh=[_fresh("a")],
        accepted_stale=[
            _accepted_stale("exports_usd_mn_monthly", date(2026, 6, 1)),
            _accepted_stale("imports_usd_mn_monthly", date(2026, 3, 1)),
            _accepted_stale("tax_gdp_ratio", date(2021, 12, 31)),  # NOT chart-feeding
        ],
    )
    _level, title, message, _fields = format_digest(report, is_heartbeat_day=True)
    assert title.startswith("Freshness sentinel — all")  # heartbeat/all-fresh shape
    assert "Chart-feeding, parked:" in message
    assert "exports_usd_mn_monthly (2026-06)" in message
    assert "imports_usd_mn_monthly (2026-03)" in message
    # A non-chart-feeding accepted-stale id must NOT appear in this line.
    parked_line = next(ln for ln in message.split("\n") if ln.startswith("Chart-feeding, parked:"))
    assert "tax_gdp_ratio" not in parked_line


def test_m5_no_parked_line_when_no_accepted_stale_chart_feeding_overlap():
    report = FreshnessReport(
        fresh=[_fresh("a")],
        accepted_stale=[_accepted_stale("tax_gdp_ratio", date(2021, 12, 31))],
    )
    _level, _title, message, _fields = format_digest(report, is_heartbeat_day=True)
    assert "Chart-feeding, parked:" not in message


# --- R3 (2026-08-08 re-review): the parked line is HEARTBEAT-DAY-gated, not
# --- breach-branch-gated -- it must appear on BOTH digest shapes on the
# --- real calendar heartbeat day, and on NEITHER shape on any other day. ---


def test_r3_parked_line_appears_on_breach_branch_on_heartbeat_day():
    """The re-reviewer's finding: the original M5 fix only ever attached the
    line to the n_breach==0 branch, which is "effectively never" reached on
    an ACTUAL heartbeat day (most heartbeat days have at least one breach,
    routing through this branch instead). Must appear here too."""
    report = FreshnessReport(
        breaches=[_breach("some_internal_metric")],
        accepted_stale=[_accepted_stale("exports_usd_mn_monthly", date(2026, 6, 1))],
    )
    _level, title, message, _fields = format_digest(report, is_heartbeat_day=True)
    assert title.startswith("Freshness sentinel — ") and "stale metric" in title  # breach shape
    assert "Chart-feeding, parked:" in message
    assert "exports_usd_mn_monthly (2026-06)" in message


def test_r3_parked_line_appears_on_no_breach_branch_on_heartbeat_day():
    report = FreshnessReport(
        fresh=[_fresh("a")],
        accepted_stale=[_accepted_stale("exports_usd_mn_monthly", date(2026, 6, 1))],
    )
    _level, _title, message, _fields = format_digest(report, is_heartbeat_day=True)
    assert "Chart-feeding, parked:" in message


def test_r3_parked_line_absent_on_breach_branch_when_not_heartbeat_day():
    report = FreshnessReport(
        breaches=[_breach("some_internal_metric")],
        accepted_stale=[_accepted_stale("exports_usd_mn_monthly", date(2026, 6, 1))],
    )
    _level, _title, message, _fields = format_digest(report, is_heartbeat_day=False)
    assert "Chart-feeding, parked:" not in message


def test_r3_parked_line_absent_on_no_breach_branch_when_not_heartbeat_day():
    report = FreshnessReport(
        fresh=[_fresh("a")],
        accepted_stale=[_accepted_stale("exports_usd_mn_monthly", date(2026, 6, 1))],
    )
    _level, _title, message, _fields = format_digest(report, is_heartbeat_day=False)
    assert "Chart-feeding, parked:" not in message


def test_r3_default_is_heartbeat_day_false_when_caller_omits_it():
    """Backward-compatible default: callers that don't know about heartbeat
    days (or just want the plain digest) get no parked line by default."""
    report = FreshnessReport(
        fresh=[_fresh("a")],
        accepted_stale=[_accepted_stale("exports_usd_mn_monthly", date(2026, 6, 1))],
    )
    _level, _title, message, _fields = format_digest(report)
    assert "Chart-feeding, parked:" not in message


def test_r3_full_breach_digest_with_parked_line_stays_under_discord_2000_char_cap():
    """2026-08-08 review R3 budget note: the reviewer measured a full
    25-line breach digest close to Discord's 2000-char embed-description
    ceiling BEFORE the parked line existed. Pin the worst realistic case --
    every real chart-feeding id breaching (the longest real metric_ids,
    now 17 since PR-C added m2_growth_yoy_monthly) plus enough other
    breaches to fill the 25-line cap, plus both known chart-feeding/
    accepted-stale ids parked -- comfortably under budget."""
    import sentinel.report as report_mod

    chart_ids = sorted(report_mod.CHART_FEEDING_METRIC_IDS)  # len computed, not hardcoded
    chart = [_breach(mid, "monthly", 300 - i) for i, mid in enumerate(chart_ids)]
    other = [_breach(f"some_other_internal_metric_id_{i}", "daily", 50 - i) for i in range(20)]
    report = FreshnessReport(
        breaches=chart + other,
        accepted_stale=[
            _accepted_stale("exports_usd_mn_monthly", date(2026, 6, 1)),
            _accepted_stale("imports_usd_mn_monthly", date(2026, 3, 1)),
        ],
    )
    _level, _title, message, _fields = format_digest(report, is_heartbeat_day=True)
    assert "Chart-feeding, parked:" in message
    assert len(message) <= 2000, f"digest is {len(message)} chars, over Discord's cap: {message!r}"


# --- BRIEF-SURFACED tier: the daily-table sibling of chart-feeding ----------


def test_brief_surfaced_breaches_are_listed_above_other_but_below_chart_feeding():
    """The real bug this closes: CPI breaches (general_inflation is a
    BRIEF_SURFACED id via macro_cpi_headline's alias family -- use a
    confirmed real id here) printed at digest rank 28-54 inside a flat
    "…and 37 more" line. They must now sit in their own above-the-fold
    section, ordered after chart-feeding but before internal-only ids."""
    report = FreshnessReport(
        breaches=[
            _breach("some_internal_parity_metric", "quarterly", 200),
            _breach("banking_npl_pct", "quarterly", 190),          # BRIEF_SURFACED
            _breach("remittance_usd_mn_monthly", "monthly", 60),   # CHART_FEEDING
        ],
    )
    _level, _title, message, _fields = format_digest(report)
    chart_idx = message.index("CHART-FEEDING")
    brief_idx = message.index("BRIEF-SURFACED")
    npl_idx = message.index("banking_npl_pct")
    other_idx = message.index("some_internal_parity_metric")
    assert chart_idx < brief_idx < npl_idx < other_idx


def test_brief_surfaced_breaches_are_never_dropped_by_the_other_budget():
    """Mirrors the chart-feeding guarantee: BRIEF_SURFACED ids must appear in
    full even when a large "Other" tier would otherwise have crowded a
    shared line-count budget."""
    import sentinel.report as report_mod

    brief_ids = sorted(report_mod.BRIEF_SURFACED_METRIC_IDS)[:10]
    brief = [_breach(mid, "daily", 50 - i) for i, mid in enumerate(brief_ids)]
    other = [_breach(f"internal_parity_{i}", "daily", 30 - i) for i in range(40)]
    report = FreshnessReport(breaches=brief + other)
    _level, _title, message, fields = format_digest(report)
    for mid in brief_ids:
        assert f"`{mid}`" in message
    assert fields["Breached"] == str(len(brief) + len(other))
    assert len(message) <= 2000


def test_ids_in_both_chart_feeding_and_brief_surfaced_appear_once_under_chart():
    """A metric_id could in principle belong to both sets -- must not be
    printed twice."""
    import sentinel.report as report_mod

    shared_id = next(iter(report_mod.CHART_FEEDING_METRIC_IDS))
    report = FreshnessReport(breaches=[_breach(shared_id, "monthly", 90)])
    _level, _title, message, _fields = format_digest(report)
    assert message.count(f"`{shared_id}`") == 1
    assert "BRIEF-SURFACED" not in message


# --- future-dated as_of: flagged, never silently discarded ------------------


def _future(mid, days_in_future=1800):
    return MetricFreshness(
        metric_id=mid, cadence="fiscal_year", latest_as_of=date(2031, 12, 31),
        latest_ingested_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        age_days=-days_in_future, breach=True, tables=("metric_history",),
    )


def test_should_send_on_future_dated_even_with_no_breaches_and_not_heartbeat():
    """A NEW future-dated as_of is itself worth alerting on -- must not wait
    for the weekly heartbeat to surface a mis-parse."""
    report = FreshnessReport(fresh=[_fresh("a")], future_dated=[_future("some_new_metric")])
    assert should_send(report, is_heartbeat_day=False) is True


def test_should_stay_silent_when_only_accepted_future_dated_rows_exist():
    """HIGH-2 (2026-08-22 round-1 review): sentinel.freshness.assess excludes
    ACCEPTED_FUTURE_DATED_METRIC_IDS (debt_gdp_ratio's known mis-parse)
    before this report is even built, so a run with ONLY that known,
    already-diagnosed anomaly must produce an EMPTY future_dated list and
    stay silent on a non-heartbeat day -- never a daily nag about a defect
    that's already understood."""
    report = FreshnessReport(fresh=[_fresh("a")], future_dated=[])
    assert should_send(report, is_heartbeat_day=False) is False


def test_future_dated_block_appears_in_heartbeat_digest():
    report = FreshnessReport(fresh=[_fresh("a")], future_dated=[_future("debt_gdp_ratio")])
    level, title, message, fields = format_digest(report)
    assert level == "warning"  # a future-dated anomaly upgrades an otherwise-quiet day
    assert "future-dated" in title.lower()
    assert "FUTURE-DATED" in message
    assert "debt_gdp_ratio" in message
    assert fields["Future-dated"] == "1"


def test_future_dated_block_appears_alongside_a_normal_breach_digest():
    report = FreshnessReport(
        breaches=[_breach("dsex")],
        future_dated=[_future("debt_gdp_ratio")],
    )
    _level, title, message, fields = format_digest(report)
    assert "future-dated" in title.lower()
    assert "FUTURE-DATED" in message
    assert "debt_gdp_ratio" in message
    assert fields["Future-dated"] == "1"


def test_no_future_dated_metrics_omits_the_block_entirely():
    report = FreshnessReport(breaches=[_breach("dsex")])
    _level, _title, message, fields = format_digest(report)
    assert "FUTURE-DATED" not in message
    assert "Future-dated" not in fields
