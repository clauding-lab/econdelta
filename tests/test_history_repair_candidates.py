"""Primary source facts, transformations and bounded manifest construction."""

from pathlib import Path

import pytest

from scripts.history_repair_candidates import source_observations
from scripts.repair_observation_history import RepairConflict

ROOT = Path(__file__).resolve().parents[1]


def test_source_periods_values_and_aligned_ratios():
    facts = source_observations(ROOT)
    assert facts["deposits_of_the_system"]["2026-05-31"]["value"] == 2041692.7
    assert facts["deposits_of_the_system"]["2026-06-30"]["value"] == 2079911.1
    assert "2026-07-31" not in facts["deposits_of_the_system"]
    assert facts["monthly_import_lc_opening"]["2026-06-30"]["value"] == 6198.59
    assert facts["monthly_import_lc_opening"]["2026-07-31"]["value"] == 6901.7
    assert facts["monthly_import_lc_opening"]["2026-05-31"]["value"] == 6212.75
    assert facts["fiscal_bank_borrow_trn"]["2026-06-30"]["value"] == pytest.approx(1.655382)
    assert facts["crr_utilisation_pct"]["2026-06-30"]["value"] == 5.1012
    assert "2026-07-31" not in facts["crr_utilisation_pct"]
    assert facts["gdp_growth_fy_pct"]["2025-06-30"]["value"] == 3.49
    assert facts["gdp_growth_fy_pct"]["2026-06-30"]["release_status"] == "provisional"
    assert "slr_utilisation_pct" not in facts  # numerator period not proven here
    assert not any("cpi" in key or "inflation" in key for key in facts)


def test_missing_or_changed_primary_source_refuses_plan(tmp_path):
    with pytest.raises(RepairConflict):
        source_observations(tmp_path)
