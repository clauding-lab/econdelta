from datetime import date

from media_screen.types import SKIP_REASONS, Skip


def test_skip_is_frozen_and_holds_reason():
    s = Skip("gross_npl_ratio", 32.26, date(2026, 3, 31), "matches-current-data")
    assert s.metric_id == "gross_npl_ratio" and s.reason == "matches-current-data"


def test_skip_reasons_are_the_five_known():
    assert SKIP_REASONS == frozenset({
        "out-of-range", "no-period", "matches-current-data",
        "older-period", "already-in-review",
    })
