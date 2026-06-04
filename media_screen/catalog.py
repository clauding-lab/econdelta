"""The BB metric catalog the screen matches press numbers against.

Spec D2 says screen "everything", but a press number is only actionable if we
know which metric_id it maps to and a sensible rounding tolerance. This overlay
seeds the headline figures the press actually prints; extend it as real
candidate volume reveals more. metric_id is the EconDelta indicator id — alias
propagation (PR #65) carries an approved override to the brief keys.
"""
from __future__ import annotations

from media_screen.types import MetricSpec

# (metric_id, press_names, tolerance-in-unit, valid_range). valid_range is the
# unit guard: a ratio is a percentage, so a Tk-crore amount mislabelled as the
# ratio (e.g. NPL "Tk 5.89 lakh crore" = 588704) falls outside [0, 50] and is
# rejected. Ratio metrics get tight percent bounds; the reserves amount-metric is
# left permissive (relies on the headline-only extraction prompt instead).
_CATALOG: tuple[MetricSpec, ...] = (
    MetricSpec("gross_npl_ratio", ("NPL ratio", "non-performing loan", "default loan"), 0.05, (0.0, 50.0)),
    MetricSpec("banking_sector_crar", ("CAR", "CRAR", "capital adequacy"), 0.05, (0.0, 40.0)),
    MetricSpec(
        "fx_reserve_gross_and_bpm6",
        ("gross reserves", "forex reserves", "foreign exchange reserves"),
        0.05,
        (0.0, 1e9),
    ),
    MetricSpec("point_to_point_inflation", ("inflation", "point-to-point inflation", "CPI"), 0.05, (0.0, 30.0)),
    MetricSpec(
        "private_sector_credit_yoy_pct",
        ("private sector credit growth", "credit growth"),
        0.05,
        (0.0, 30.0),
    ),
)


def load_catalog() -> list[MetricSpec]:
    """Return the BB metrics the screen covers, with press aliases + tolerances."""
    return list(_CATALOG)
