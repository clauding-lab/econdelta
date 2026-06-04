from datetime import date

from media_screen.filter import classify
from media_screen.types import Candidate, Extracted, Skip

P_AS_OF = date(2025, 9, 30)


def _ex(value, period):
    return Extracted("NPL", value, period, "quote", "http://x", "tbsnews")


def test_no_period_is_skipped_with_reason():
    r = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(32.26, None), tolerance=0.05)
    assert isinstance(r, Skip) and r.reason == "no-period"


def test_fresher_period_is_a_candidate():
    c = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(32.26, date(2026, 3, 31)), tolerance=0.05)
    assert isinstance(c, Candidate) and c.kind == "fresher_period"
    assert c.press_value == 32.26 and c.press_as_of == date(2026, 3, 31)


def test_same_period_material_diff_is_a_conflict():
    c = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(35.50, P_AS_OF), tolerance=0.05)
    assert isinstance(c, Candidate) and c.kind == "same_period_conflict"


def test_same_period_within_tolerance_is_matches_current():
    r = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(35.75, P_AS_OF), tolerance=0.05)
    assert isinstance(r, Skip) and r.reason == "matches-current-data"


def test_older_period_is_skipped_with_reason():
    r = classify("gross_npl_ratio", 35.73, P_AS_OF, _ex(34.0, date(2025, 6, 30)), tolerance=0.05)
    assert isinstance(r, Skip) and r.reason == "older-period"


def test_no_parsed_value_with_dated_press_is_fresher():
    c = classify("x", None, None, _ex(10.0, date(2026, 1, 31)), tolerance=0.05)
    assert isinstance(c, Candidate) and c.kind == "fresher_period"
