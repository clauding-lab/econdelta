"""Pure-functions module for seeding macro monthly data.

No I/O, no HTTP requests, no os.environ access.
CLI / upsert path comes in Task 6.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger("seed_macro_monthly")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DOMAIN_VALUES: frozenset[str] = frozenset({
    "prices_policy",
    "credit_money",
    "external",
    "capital_market",
})

SOURCE_URL: str = "https://macro.thenazmussakib.com/"
SOURCE_ATTRIBUTION: str = "Nazmus Sakib · BB · BBS · DSE"
DEFAULT_SOURCE: str = "macro_observer_seed"

# ---------------------------------------------------------------------------
# MetricMap dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricMap:
    metric_id: str
    display_name: str
    unit: str
    domain: str
    notes: str = ""

    def __post_init__(self) -> None:
        if self.domain not in DOMAIN_VALUES:
            raise ValueError(
                f"domain {self.domain!r} is not valid; must be one of {sorted(DOMAIN_VALUES)}"
            )


# ---------------------------------------------------------------------------
# KEY_MAP — flat dict, upstream camelCase key → MetricMap
# ---------------------------------------------------------------------------

KEY_MAP: dict[str, MetricMap] = {
    # --- prices_policy ---
    "genP2P": MetricMap("point_to_point_inflation_monthly", "CPI YoY (general)", "%", "prices_policy"),
    "foodP2P": MetricMap("cpi_p2p_food_monthly", "CPI YoY (food)", "%", "prices_policy"),
    "nonFoodP2P": MetricMap("cpi_p2p_nonfood_monthly", "CPI YoY (non-food)", "%", "prices_policy"),
    "gen12M": MetricMap("cpi_12m_avg_monthly", "CPI 12-month average", "%", "prices_policy"),
    "food12M": MetricMap("cpi_12m_food_monthly", "CPI 12-month average (food)", "%", "prices_policy"),
    "nonFood12M": MetricMap("cpi_12m_nonfood_monthly", "CPI 12-month average (non-food)", "%", "prices_policy"),
    "repo": MetricMap("bb_repo_rate_monthly", "BB repo rate", "%", "prices_policy", notes="Sparse — nulls for earlier months"),
    "tbill364": MetricMap("tbill_364d_yield_monthly", "364-day T-bill yield", "%", "prices_policy"),
    "tr2y": MetricMap("yield_2y_monthly", "2Y bond yield", "%", "prices_policy"),
    "tr5y": MetricMap("yield_5y_monthly", "5Y bond yield", "%", "prices_policy"),
    "tr10y": MetricMap("yield_10y_monthly", "10Y bond yield", "%", "prices_policy"),
    "tr20y": MetricMap("yield_20y_monthly", "20Y bond yield", "%", "prices_policy"),
    # --- credit_money ---
    "domCredit": MetricMap("domestic_credit_total_monthly", "Total domestic credit", "BDT bn", "credit_money"),
    "pubCredit": MetricMap("domestic_credit_public_monthly", "Public-sector domestic credit", "BDT bn", "credit_money"),
    "privCredit": MetricMap("domestic_credit_private_monthly", "Private-sector domestic credit", "BDT bn", "credit_money"),
    "domCreditGr": MetricMap("domestic_credit_growth_yoy_monthly", "Total credit growth YoY", "%", "credit_money"),
    "privCreditGr": MetricMap("private_credit_growth_yoy_monthly", "Private credit growth YoY", "%", "credit_money"),
    "pubCreditGr": MetricMap("public_credit_growth_yoy_monthly", "Public credit growth YoY", "%", "credit_money"),
    "m1Gr": MetricMap("m1_growth_yoy_monthly", "M1 growth YoY", "%", "credit_money"),
    "m2Gr": MetricMap("m2_growth_yoy_monthly", "M2 growth YoY", "%", "credit_money"),
    # --- external ---
    "expUsd": MetricMap("exports_usd_mn_monthly", "Exports", "USD mn", "external"),
    "impUsd": MetricMap("imports_usd_mn_monthly", "Imports", "USD mn", "external"),
    "remUsd": MetricMap("remittance_usd_mn_monthly", "Remittance", "USD mn", "external"),
    "fxReserve": MetricMap("gross_reserves_usd_bn_monthly", "FX reserves (gross)", "USD bn", "external"),
    "importCov": MetricMap("import_cover_months_monthly", "Import cover", "mo", "external"),
    "bdtUsd": MetricMap("usd_bdt_mid_monthly", "BDT / USD", "BDT", "external"),
    "reer": MetricMap("reer_monthly", "REER (100 baseline)", "index", "external"),
    # --- capital_market ---
    "dsex": MetricMap("dsex_monthly", "DSEX index", "index", "capital_market"),
}

# ---------------------------------------------------------------------------
# Derived metric definition (not in KEY_MAP; produced by build_history_rows)
# ---------------------------------------------------------------------------

_DERIVED_REAL_POLICY_RATE = MetricMap(
    metric_id="real_policy_rate_monthly",
    display_name="Real policy rate",
    unit="%",
    domain="prices_policy",
    notes="Derived: BB repo rate minus headline CPI YoY (genP2P). Null-propagated.",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalise_as_of(raw: str) -> date:
    """Accept 'YYYY-MM' or 'YYYY-MM-DD' and return date(year, month, 1)."""
    raw = raw.strip()
    if len(raw) == 7:
        # 'YYYY-MM'
        try:
            year, month = raw.split("-")
            return date(int(year), int(month), 1)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Cannot parse date from {raw!r}") from exc
    elif len(raw) == 10:
        # 'YYYY-MM-DD' — clamp to day 1
        try:
            year, month, _ = raw.split("-")
            return date(int(year), int(month), 1)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Cannot parse date from {raw!r}") from exc
    else:
        raise ValueError(f"Cannot parse date from {raw!r}: expected 'YYYY-MM' or 'YYYY-MM-DD'")


def _is_numeric(value: object) -> bool:
    """Return True iff value is a real number (int or float, excluding bool)."""
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def build_history_rows(
    payload: dict,
    *,
    source: str = DEFAULT_SOURCE,
) -> list[dict]:
    """Build metric_history_monthly rows from the upstream parallel-array payload.

    Reads payload['months'] (the date axis) and each KEY_MAP key present in
    payload. Also computes the derived 'real_policy_rate_monthly' from
    payload['repo'] - payload['genP2P'].

    Skips rows where the value is None or not a real number.
    Handles off-by-one: iterates over min(len(months), len(series)).
    """
    months: list[str] = payload.get("months", [])
    rows: list[dict] = []

    # Collect regular series from KEY_MAP
    for upstream_key, metric in KEY_MAP.items():
        series = payload.get(upstream_key)
        if series is None:
            # Key not present in this payload — skip silently
            continue

        limit = min(len(months), len(series))
        for i in range(limit):
            month_str = months[i]
            value = series[i]

            if not _is_numeric(value):
                continue

            as_of_date = normalise_as_of(month_str)
            as_of_str = as_of_date.isoformat()

            rows.append({
                "metric_id": metric.metric_id,
                "as_of": as_of_str,
                "value": value,
                "source": source,
                "source_as_of": as_of_str,
            })

    # Derived: real_policy_rate = repo - genP2P
    repo_series = payload.get("repo")
    genp2p_series = payload.get("genP2P")
    if repo_series is not None and genp2p_series is not None:
        limit = min(len(months), len(repo_series), len(genp2p_series))
        for i in range(limit):
            repo_val = repo_series[i]
            genp2p_val = genp2p_series[i]

            if not _is_numeric(repo_val) or not _is_numeric(genp2p_val):
                continue

            month_str = months[i]
            as_of_date = normalise_as_of(month_str)
            as_of_str = as_of_date.isoformat()

            rows.append({
                "metric_id": "real_policy_rate_monthly",
                "as_of": as_of_str,
                "value": repo_val - genp2p_val,
                "source": source,
                "source_as_of": as_of_str,
            })

    return rows


def build_definitions_rows() -> list[dict]:
    """Build metric_definitions_monthly rows for all KEY_MAP metrics + derived."""
    rows: list[dict] = []

    for metric in KEY_MAP.values():
        rows.append({
            "metric_id": metric.metric_id,
            "display_name": metric.display_name,
            "unit": metric.unit,
            "source_url": SOURCE_URL,
            "source_attribution": SOURCE_ATTRIBUTION,
            "domain": metric.domain,
            "description": metric.display_name,
            "notes": metric.notes,
        })

    # Add derived metric
    m = _DERIVED_REAL_POLICY_RATE
    rows.append({
        "metric_id": m.metric_id,
        "display_name": m.display_name,
        "unit": m.unit,
        "source_url": SOURCE_URL,
        "source_attribution": SOURCE_ATTRIBUTION,
        "domain": m.domain,
        "description": "Real policy rate (BB repo minus headline CPI YoY)",
        "notes": m.notes,
    })

    return rows
