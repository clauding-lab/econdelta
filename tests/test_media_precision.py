"""Precision guards: the value-range/unit check that stops Tk-amounts being
classified as percent ratios (the dry-run surfaced ~13 such junk candidates)."""
from __future__ import annotations

from datetime import date

from media_screen.catalog import load_catalog
from media_screen.filter import classify
from media_screen.types import Candidate, Extracted, Skip

P_AS_OF = date(2025, 9, 30)


def _ex(value, period=date(2026, 3, 31)):
    return Extracted("NPL ratio", value, period, "quote", "http://x", "tbsnews")


def test_amount_mislabelled_as_ratio_is_rejected():
    r = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(588704.0),
                 tolerance=0.05, valid_range=(0.0, 50.0))
    assert isinstance(r, Skip) and r.reason == "out-of-range"


def test_in_range_ratio_still_classifies():
    c = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(32.26),
                 tolerance=0.05, valid_range=(0.0, 50.0))
    assert isinstance(c, Candidate) and c.kind == "fresher_period" and c.press_value == 32.26


def test_range_guard_runs_before_period_check():
    """An out-of-range value is rejected (reason=out-of-range) even with period=None,
    proving the range guard fires before the period check."""
    r = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(416482.0, period=None),
                 tolerance=0.05, valid_range=(0.0, 50.0))
    assert isinstance(r, Skip) and r.reason == "out-of-range"


def test_default_range_is_permissive():
    c = classify("x", None, None, _ex(588704.0), tolerance=0.05)
    assert isinstance(c, Candidate)


def test_every_catalog_spec_has_a_sane_range():
    for s in load_catalog():
        lo, hi = s.valid_range
        assert lo < hi, f"{s.metric_id} has an empty/inverted valid_range"
        assert lo >= 0.0, f"{s.metric_id} lower bound should be non-negative"
