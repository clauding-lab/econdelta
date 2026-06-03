from datetime import date

from media_screen.filter import classify
from media_screen.types import Extracted

P_AS_OF = date(2025, 9, 30)


def _ex(value, period):
    return Extracted("NPL", value, period, "quote", "http://x", "tbsnews")


def test_no_period_is_discarded():
    """Strict rule: an undated press number is never a candidate (the old flap)."""
    assert classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(32.26, None), tolerance=0.05) is None


def test_fresher_period_is_a_candidate():
    c = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(32.26, date(2026, 3, 31)), tolerance=0.05)
    assert c is not None and c.kind == "fresher_period"
    assert c.press_value == 32.26 and c.press_as_of == date(2026, 3, 31)


def test_same_period_material_diff_is_a_conflict():
    c = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(35.50, P_AS_OF), tolerance=0.05)
    assert c is not None and c.kind == "same_period_conflict"


def test_same_period_within_tolerance_is_not_raised():
    """35.73 vs 35.75 (rounding) at the same period is the same number — no ping."""
    assert classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(35.75, P_AS_OF), tolerance=0.05) is None


def test_no_parsed_value_with_dated_press_is_fresher():
    """Metric never parsed yet, press has a dated value → surface it as fresher_period."""
    c = classify("x", None, None, _ex(10.0, date(2026, 1, 31)), tolerance=0.05)
    assert c is not None and c.kind == "fresher_period"
