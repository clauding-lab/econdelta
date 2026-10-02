"""Observation records keep economic periods attached across selection and derivation."""

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def observation(**kwargs):
    from utils.observations import Observation

    return Observation(
        **{
            "metric_id": "gross_reserves_usd_bn",
            "value": 37.3523,
            "as_of": date(2026, 8, 31),
            "unit": "USD billion",
            "source": "bb_forex",
            "source_url": "https://example.test/reserves",
            "captured_at": NOW,
            "quality": "verified",
            "date_basis": "observation",
            "evidence": "August row",
            **kwargs,
        }
    )


def test_selection_keeps_preferred_complete_record_and_rejects_stale_preference():
    from utils.observations import select_observation

    preferred = observation()
    other = observation(value=35.5, as_of=date(2026, 7, 31), source="monthly_pdf")
    assert select_observation([preferred, other], today=NOW.date()) == preferred
    assert (
        select_observation([replace(other, quality="held"), preferred], today=NOW.date())
        == preferred
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"as_of": date(2031, 12, 31)},
        {"as_of": None},
        {"value": True},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": float("-inf")},
        {"quality": "unavailable"},
        {"date_basis": "unknown"},
        {"date_basis": "writer_confirmation"},
    ],
)
def test_ineligible_observation_is_never_selected(changes):
    from utils.observations import select_observation

    assert select_observation([observation(**changes)], today=NOW.date()) is None


def test_alias_conversion_carries_source_date_unit_and_dependency():
    from utils.observations import expand_aliases

    base = observation(metric_id="monthly_remittance", value=2.97)
    alias = expand_aliases({base.metric_id: base})["remit_monthly_mn"]
    assert (alias.value, alias.unit, alias.as_of, alias.source) == (
        2970.0,
        "USD million",
        base.as_of,
        base.source,
    )
    assert alias.dependencies == (base.metric_id,)
    assert alias.source_url == base.source_url


def test_ratio_requires_aligned_periods_and_preserves_dependencies():
    from utils.observations import derive_ratio

    num = observation(metric_id="balance", value=60, unit="BDT crore")
    den = observation(metric_id="deposits", value=1500, unit="BDT crore")
    result = derive_ratio("crr_utilisation_pct", num, den)
    assert (result.value, result.as_of, result.dependencies) == (
        4,
        num.as_of,
        ("balance", "deposits"),
    )
    for bad in (
        replace(den, as_of=date(2026, 7, 31)),
        replace(den, value=0),
        replace(den, value=True),
        replace(den, quality="unavailable"),
        replace(den, unit="BDT million"),
    ):
        assert derive_ratio("crr_utilisation_pct", num, bad) is None


def test_legacy_scalar_is_unavailable_without_evidence():
    from utils.observations import from_snapshot

    obs = from_snapshot("monthly_remittance", {"value": 2.97}, captured_at=NOW)
    assert obs.quality == "unavailable"
    assert obs.date_basis == "unknown"
    assert obs.as_of is None


def test_stale_vintage_uses_existing_trading_day_policy_at_adapter_boundary():
    import aggregate_latest as agg
    from sentinel.freshness import is_breach

    # Thursday's quote remains eligible through Friday and Saturday and one holiday.
    for today in (date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27)):
        assert not is_breach(date(2026, 9, 24), "daily", today, {date(2026, 9, 27)})
    domains = {
        "macro": {
            "gross_npl_ratio": {
                "value": 30,
                "source_as_of": "2026-06-30",
                "cadence": "quarterly",
                "_provenance": "deterministic",
                "scraped_at": NOW.isoformat(),
            }
        }
    }
    result = agg._build_observations(
        {}, domains, {}, now=NOW, holidays=set(), yield_values={}, yield_dates={}
    )
    assert (
        result["gross_npl_ratio"].quality == "verified"
    )  # 87 days < existing 165-day vintage grace


def test_policy_confirmation_is_explicit_and_old_capture_is_not_restamped():
    from utils.observations import from_snapshot

    snap = {"value": 9.5, "_provenance": "deterministic", "scraped_at": "2026-09-20T23:00:00+00:00"}
    obs = from_snapshot("policy_rate_repo", snap, captured_at=NOW)
    assert (obs.as_of, obs.date_basis) == (date(2026, 9, 20), "writer_confirmation")
    assert from_snapshot("general_inflation", snap, captured_at=NOW).quality == "unavailable"


def test_domain_adapter_never_borrows_equal_number_from_another_source():
    import aggregate_latest as agg

    domains = {
        "macro": {
            "monthly_remittance": {
                "value": 2.97,
                "_provenance": "deterministic",
                "cadence": "monthly",
            },
            "monthly_export": {
                "value": 2.97,
                "source_as_of": "2026-07-31",
                "_provenance": "deterministic",
                "cadence": "monthly",
            },
            "call_money_rate": {
                "value": {"1D": 9.5, "7D": 10},
                "source_as_of": "2026-09-24",
                "_provenance": "deterministic",
                "cadence": "daily",
            },
        }
    }
    obs = agg._build_observations(
        {}, domains, {}, now=NOW, holidays=set(), yield_values={}, yield_dates={}
    )
    assert obs["remit_monthly_mn"].as_of is None
    assert obs["call_money_rate_7d"].as_of == date(2026, 9, 24)
    assert obs["call_money_rate_7d"].dependencies == ("call_money_rate",)


def test_alias_cannot_create_nonfinite_value_from_finite_parent():
    from utils.observations import expand_aliases

    base = observation(metric_id="monthly_remittance", value=1e308)
    child = expand_aliases({base.metric_id: base})["remit_monthly_mn"]
    assert child.value is None
    assert child.quality == "unavailable"


def test_projection_removes_orphan_aliases_and_competing_source_evidence():
    import aggregate_latest as agg

    selected = observation(metric_id="usd_bdt_exchange_rate", value=123.22, as_of=date(2026, 9, 24))
    data = {"usd_bdt_exchange_rate": 121, "macro_cpi_headline": 8.6, "news": "Keep context"}
    domains = {
        "fx": {
            selected.metric_id: {
                "value": 121,
                "_artifact_sha256": "competing-pdf",
                "_parse_strategy": "pdf_table",
                "_provenance": "deterministic",
                "source_url": "https://example.test/competing",
                "previous_value": 120,
                "change_pct": 0.8,
            }
        }
    }
    agg._project_observations(data, domains, {selected.metric_id: selected})
    assert "macro_cpi_headline" not in data
    assert data["news"] == "Keep context"
    record = domains["fx"][selected.metric_id]
    assert record["source_url"] == selected.source_url
    assert "_artifact_sha256" not in record
    assert "_parse_strategy" not in record
    assert "change_pct" not in record
