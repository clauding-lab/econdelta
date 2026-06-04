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
    # --- 2026-06-04 expansion: press-reportable BB headline figures (adversarially
    # vetted). The matcher is last-writer-wins ({n.lower(): spec}), so every alias must
    # be UNIQUE across all specs and specific enough not to false-match — see
    # tests/test_media_catalog.py::test_no_alias_collisions. ---
    # Rates (ratio %): qualified so the rate/yield family can't collide.
    MetricSpec("policy_rate_repo", ("policy repo rate", "policy interest rate", "central bank policy rate"), 0.05, (0.0, 30.0)),
    MetricSpec("call_money_rate", ("call money rate", "interbank call money rate", "overnight call money rate"), 0.05, (0.0, 30.0)),
    MetricSpec("tbill_91d_yield_pct", ("91-day treasury bill yield", "91-day t-bill yield", "91-day t-bill rate"), 0.05, (0.0, 30.0)),
    # Inflation: "food inflation" is the distinct headlined component. (Bare "inflation"/
    # "CPI" belong to point_to_point_inflation above; general_inflation is the SAME BB source.)
    MetricSpec("food_inflation", ("food inflation", "food inflation rate"), 0.05, (0.0, 30.0)),
    # External sector (amount, $bn): the FY-to-date marquee figures the press headlines.
    MetricSpec("fy_remittance", ("remittance inflow", "fiscal-year remittance"), 0.15, (0.0, 1000.0)),
    MetricSpec("fy_export", ("export earnings", "merchandise exports", "fiscal-year export earnings"), 0.2, (0.0, 1000.0)),
    # Fiscal (amount, Tk crore FYTD).
    MetricSpec("tax_revenue", ("nbr revenue collection", "tax revenue collection", "nbr tax collection"), 1500.0, (0.0, 1e7)),
    # FX rate (amount, BDT/USD). CAVEAT: the tracked value is the BB crawling-peg MID rate;
    # the press often quotes diverging SEGMENT rates (interbank/selling/kerb) that the
    # extraction's overall-only rule may reject — expect more skips than candidates here.
    MetricSpec("usd_bdt_exchange_rate", ("taka-dollar exchange rate", "taka per dollar", "interbank dollar rate"), 0.25, (80.0, 200.0)),
)


def load_catalog() -> list[MetricSpec]:
    """Return the BB metrics the screen covers, with press aliases + tolerances."""
    return list(_CATALOG)
