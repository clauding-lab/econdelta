"""Tests for the derived CRR / SLR reserve-utilisation ratios (S2).

EconDelta scrapes only the absolute LEVELS from the BB MEI bulletin:
  - ``deposits_held_with_bb_crr``            (CRR balance, BDT crore)
  - ``excess_liquid_asset_total_minimum``    (SLR surplus over minimum, BDT crore)
  - ``deposits_of_the_system``               (total system deposits, BDT crore)

There is no scraped maintenance-% cell, so ``_compute_reserve_utilisation``
mints the two ratios at runtime inside ``_build_v3_blocks`` (before the
Supabase writer's scalar-only filter) so they land in ``metric_history``.

Each ratio = numerator / denominator × 100, labelled by what it actually
divides (% of system deposits) — NOT a regulated statutory ratio, so no
policy constant is hardcoded. The math must be null-safe: a missing month or
zero denominator yields NO key (a missing metric), never a bogus 9999%.
"""
from __future__ import annotations

from aggregate_latest import (
    DERIVED_DEFINITION_SEEDS,
    _build_definition_seeds,
    _compute_reserve_utilisation,
)


def test_crr_utilisation_is_balance_over_deposits_pct():
    """crr_utilisation_pct = deposits_held_with_bb_crr / deposits_of_the_system × 100."""
    data = {
        "deposits_held_with_bb_crr": 60_000.0,
        "deposits_of_the_system": 1_500_000.0,
    }
    _compute_reserve_utilisation(data)
    assert data["crr_utilisation_pct"] == 4.0  # 60000 / 1_500_000 × 100


def test_slr_utilisation_is_excess_liquid_over_deposits_pct():
    """slr_utilisation_pct = excess_liquid_asset_total_minimum / deposits_of_the_system × 100."""
    data = {
        "excess_liquid_asset_total_minimum": 300_000.0,
        "deposits_of_the_system": 1_500_000.0,
    }
    _compute_reserve_utilisation(data)
    assert data["slr_utilisation_pct"] == 20.0  # 300000 / 1_500_000 × 100


def test_both_ratios_computed_together_with_rounding():
    """A realistic month: both ratios minted, rounded to 4 dp."""
    data = {
        "deposits_held_with_bb_crr": 72_345.0,
        "excess_liquid_asset_total_minimum": 215_678.0,
        "deposits_of_the_system": 1_812_900.0,
    }
    _compute_reserve_utilisation(data)
    assert data["crr_utilisation_pct"] == round(72_345.0 / 1_812_900.0 * 100, 4)
    assert data["slr_utilisation_pct"] == round(215_678.0 / 1_812_900.0 * 100, 4)


def test_missing_numerator_yields_no_key():
    """A missing CRR balance (month not yet scraped) → no crr key, not a 0%."""
    data = {"deposits_of_the_system": 1_500_000.0}
    _compute_reserve_utilisation(data)
    assert "crr_utilisation_pct" not in data
    assert "slr_utilisation_pct" not in data


def test_missing_denominator_yields_no_key():
    """A missing deposits base → both ratios absent (never divide by missing)."""
    data = {
        "deposits_held_with_bb_crr": 60_000.0,
        "excess_liquid_asset_total_minimum": 300_000.0,
    }
    _compute_reserve_utilisation(data)
    assert "crr_utilisation_pct" not in data
    assert "slr_utilisation_pct" not in data


def test_zero_denominator_guarded_no_division():
    """A zero deposits base must NOT divide-by-zero or emit a bogus 9999%."""
    data = {
        "deposits_held_with_bb_crr": 60_000.0,
        "excess_liquid_asset_total_minimum": 300_000.0,
        "deposits_of_the_system": 0.0,
    }
    _compute_reserve_utilisation(data)
    assert "crr_utilisation_pct" not in data
    assert "slr_utilisation_pct" not in data


def test_negative_denominator_guarded():
    """A nonsensical negative deposits base is rejected (denominator must be > 0)."""
    data = {
        "deposits_held_with_bb_crr": 60_000.0,
        "deposits_of_the_system": -1.0,
    }
    _compute_reserve_utilisation(data)
    assert "crr_utilisation_pct" not in data


def test_non_numeric_input_skipped():
    """A string/None level (e.g. a needs-review snapshot) is skipped cleanly."""
    data = {
        "deposits_held_with_bb_crr": "n/a",
        "excess_liquid_asset_total_minimum": None,
        "deposits_of_the_system": 1_500_000.0,
    }
    _compute_reserve_utilisation(data)
    assert "crr_utilisation_pct" not in data
    assert "slr_utilisation_pct" not in data


def test_bool_input_rejected():
    """bool is a subclass of int — must NOT be treated as a numeric level."""
    data = {
        "deposits_held_with_bb_crr": True,
        "deposits_of_the_system": 1_500_000.0,
    }
    _compute_reserve_utilisation(data)
    assert "crr_utilisation_pct" not in data


def test_idempotent_does_not_overwrite_preset_ratio():
    """If a ratio is already present (prior pass / hand-override), leave it."""
    data = {
        "deposits_held_with_bb_crr": 60_000.0,
        "deposits_of_the_system": 1_500_000.0,
        "crr_utilisation_pct": 99.99,
    }
    _compute_reserve_utilisation(data)
    assert data["crr_utilisation_pct"] == 99.99


def test_partial_month_computes_only_the_available_ratio():
    """CRR legs present but SLR excess absent → crr minted, slr absent."""
    data = {
        "deposits_held_with_bb_crr": 60_000.0,
        "deposits_of_the_system": 1_500_000.0,
    }
    _compute_reserve_utilisation(data)
    assert data["crr_utilisation_pct"] == 4.0
    assert "slr_utilisation_pct" not in data


def test_utilisation_can_exceed_100_pct():
    """Excess liquid assets can be large relative to deposits — > 100% is valid
    and must round through (valid_range allows it, no clamping here)."""
    data = {
        "excess_liquid_asset_total_minimum": 1_800_000.0,
        "deposits_of_the_system": 1_500_000.0,
    }
    _compute_reserve_utilisation(data)
    assert data["slr_utilisation_pct"] == 120.0


def test_definition_seeds_include_both_derived_ratios():
    """Both derived ids get a metric_definitions seed so the catalog + Supabase
    definitions stay in sync with the values minted in _build_v3_blocks."""
    seeds = _build_definition_seeds({"indicators": []})
    by_id = {s["metric_id"]: s for s in seeds}
    assert "crr_utilisation_pct" in by_id
    assert "slr_utilisation_pct" in by_id
    assert by_id["crr_utilisation_pct"]["unit"] == "%"
    assert by_id["slr_utilisation_pct"]["cadence"] == "monthly"


def test_derived_definition_seed_ids_match_computation_ids():
    """Guard: the definition-seed ids must exactly match the ids minted by the
    computation — a typo would seed a definition no value ever populates."""
    from aggregate_latest import RESERVE_UTIL_DERIVED

    seed_ids = {d["metric_id"] for d in DERIVED_DEFINITION_SEEDS}
    assert seed_ids == set(RESERVE_UTIL_DERIVED.keys())
