"""Aggregates latest snapshot from each scraper into data/latest.json — the canonical
file The Brief reads. Atomic write, Pydantic-validated, with per-source status."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from utils.notifier import notify
from utils.opus_review import archive_latest, load_history, review_data
from utils.schema import (
    Alert,
    CommoditySnapshot,
    DseSnapshot,
    ForexSnapshot,
    FreshnessByCadence,
    FreshnessSummary,
    LatestBundle,
    SourceStatus,
)

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
ARCHIVE_DIR = DATA_DIR / "archive"
CONFIG_PATH = REPO_ROOT / "config" / "sources.json"
SOURCES_V3_PATH = REPO_ROOT / "config" / "sources-v3.json"

STALE_THRESHOLD_HOURS = 24.0

STALE_THRESHOLDS_HOURS_BY_CADENCE: dict[str, float] = {
    "daily": 24.0,
    "weekly": 8 * 24.0,       # 192h
    "monthly": 35 * 24.0,     # 840h
    "quarterly": 100 * 24.0,  # 2400h
    "fy": 400 * 24.0,         # 9600h
}

# Cumulative-figure guard: a fiscal-year-to-date total can only rise within a FY.
CUMULATIVE_DROP_TOLERANCE = 0.05   # >5% same-FY drop ⇒ implausible
FISCAL_YEAR_START_MONTH = 7        # Bangladesh FY = July–June
# Granular Opus reject: quarantine up to this many flagged fields; more ⇒ hard reject.
MAX_QUARANTINE_FIELDS = 5

logger = logging.getLogger("aggregate_latest")

# Derived reserve-utilisation ratios (S2). Computed at runtime from the
# already-scraped BB MEI scalars below — EconDelta has NO scraped maintenance-%
# cell, so these are minted in `_build_v3_blocks` and land in metric_history
# under their own ids. The exact statutory CRR/SLR bases are policy constants
# that shift, so each ratio is labelled by what it ACTUALLY divides (no
# hardcoded statutory rate): the held/excess balance expressed as a % of total
# system deposits, NOT the regulated maintenance ratio.
RESERVE_UTIL_DERIVED: dict[str, tuple[str, str]] = {
    # derived_id -> (numerator_id, denominator_id)
    "crr_utilisation_pct": ("deposits_held_with_bb_crr", "deposits_of_the_system"),
    "slr_utilisation_pct": ("excess_liquid_asset_total_minimum", "deposits_of_the_system"),
}


SCRAPER_SPEC = {
    # key -> (subdir, schema_class, sources.json key for URL lookup)
    "bb_forex": ("bb_forex", ForexSnapshot, "bb_exchange_rates"),
    "dse_market": ("dse_market", DseSnapshot, "dse_market_summary"),
    "commodity_prices": ("commodity_prices", CommoditySnapshot, None),
}


def find_latest_snapshot(subdir: Path) -> Path | None:
    """Return the newest JSON file in subdir (by filename lexicographic — dates sort correctly).

    Ignores .tmp files and any non-JSON files.
    """
    if not subdir.exists():
        return None
    candidates = sorted(
        (p for p in subdir.glob("*.json") if not p.name.endswith(".tmp.json")),
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_snapshot(path: Path, schema_class: type) -> Any:
    """Parse JSON file into Pydantic model. Returns None on error."""
    try:
        with path.open() as f:
            data = json.load(f)
        return schema_class.model_validate(data)
    except (json.JSONDecodeError, ValidationError, FileNotFoundError, OSError) as e:
        logger.warning("failed to load %s: %s", path, e)
        return None


def compute_status(snapshot: Any, url: str | None, now: datetime) -> SourceStatus:
    """Derive SourceStatus from a loaded snapshot + current time."""
    if snapshot is None:
        return SourceStatus(
            status="missing",
            last_success=None,
            age_hours=None,
            url=url,
            error="no snapshot found or validation failed",
        )
    scraped_at = snapshot.scraped_at
    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=timezone.utc)
    age_hours = (now - scraped_at).total_seconds() / 3600.0
    status = "ok" if age_hours <= STALE_THRESHOLD_HOURS else "stale"
    return SourceStatus(
        status=status,
        last_success=scraped_at,
        age_hours=round(age_hours, 2),
        url=url,
        error=None,
    )


def flatten_data(snapshots: dict[str, Any]) -> dict[str, Any]:
    """Flatten the three snapshots into a single dict for The Brief to consume."""
    data: dict[str, Any] = {}

    forex = snapshots.get("bb_forex")
    if forex is not None:
        data["usd_bdt_mid"] = forex.rates.usd_bdt_mid
        data["usd_bdt_buy"] = forex.rates.usd_bdt_buy
        data["usd_bdt_sell"] = forex.rates.usd_bdt_sell
        data["eur_bdt"] = forex.rates.eur_bdt
        data["gbp_bdt"] = forex.rates.gbp_bdt
        if forex.reserves is not None:
            data["gross_reserves_usd_bn"] = forex.reserves.gross_reserves_usd_bn
            data["import_cover_months"] = forex.reserves.import_cover_months
            data["reserves_date"] = forex.reserves.reserves_date.isoformat()

    dse = snapshots.get("dse_market")
    if dse is not None:
        data["trading_day"] = dse.trading_day
        if dse.indices is not None:
            data["dsex"] = dse.indices.dsex
            data["dsex_change"] = dse.indices.dsex_change
            data["dsex_change_pct"] = dse.indices.dsex_change_pct
            data["ds30"] = dse.indices.ds30
            data["dses"] = dse.indices.dses
        if dse.market is not None:
            data["turnover_crore"] = dse.market.turnover_crore
            data["total_trades"] = dse.market.total_trades
            data["advancing"] = dse.market.advancing
            data["declining"] = dse.market.declining
            data["unchanged"] = dse.market.unchanged

    commodities = snapshots.get("commodity_prices")
    if commodities is not None:
        for key, cp in commodities.prices.items():
            unit_suffix = f"{cp.currency.lower()}_{cp.unit.replace(' ', '_')}"
            data[f"{key}_{unit_suffix}"] = cp.price
        change_pcts = {
            key: cp.change_pct
            for key, cp in commodities.prices.items()
            if cp.change_pct is not None
        }
        if change_pcts:
            data["commodity_change_pct"] = change_pcts

    return data


def _load_v3_registry() -> list[dict]:
    """Load the v3 indicator registry from config/sources-v3.json.

    Returns an empty list if the file does not exist (pre-v3 installs).
    """
    if not SOURCES_V3_PATH.exists():
        return []
    try:
        return json.loads(SOURCES_V3_PATH.read_text()).get("indicators", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("failed to load v3 registry: %s", e)
        return []


def _load_v3_snapshot(indicator_id: str) -> dict | None:
    """Return the latest per-indicator snapshot dict, or None if unavailable."""
    d = DATA_DIR / indicator_id
    if not d.exists():
        return None
    candidates = sorted(d.glob("*.json"), reverse=True)
    if not candidates:
        return None
    try:
        return json.loads(candidates[0].read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("failed to load v3 snapshot for %s: %s", indicator_id, e)
        return None


def _is_bad_snapshot(snapshot: dict) -> bool:
    """True if the snapshot represents a failed parse (sentinel 0.0 or missing value)."""
    if snapshot.get("_provenance") == "needs_review":
        return True
    if snapshot.get("_parse_strategy") == "extract_failed":
        return True
    if snapshot.get("value") in (None, 0, 0.0):
        return True
    return False


def _load_last_good_snapshot(indicator_id: str, *, max_days_back: int = 60) -> dict | None:
    """Walk back through this indicator's per-day snapshots for the most recent good one.

    A 'good' snapshot is one where _is_bad_snapshot() is False — i.e. real
    extracted data, not the 0.0 placeholder the parser writes when extraction
    fails. Returns the snapshot dict (with the original date in _stale_from
    annotation) or None if no good snapshot exists in the lookback window.
    """
    d = DATA_DIR / indicator_id
    if not d.exists():
        return None
    candidates = sorted(d.glob("*.json"), reverse=True)
    cutoff_age_days = max_days_back
    today = datetime.now(timezone.utc).date()
    for path in candidates:
        try:
            blob = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if _is_bad_snapshot(blob):
            continue
        # Check it's not too far in the past
        try:
            scraped = datetime.fromisoformat(
                blob["scraped_at"].replace("Z", "+00:00")
            ).date()
            if (today - scraped).days > cutoff_age_days:
                return None  # too old, give up (history is sorted newest-first)
        except (KeyError, ValueError):
            continue
        # Annotate with stale-fallback metadata
        blob["_provenance"] = "stale_fallback"
        blob["_stale_from"] = path.stem  # e.g. "2026-04-29"
        return blob
    return None


def _prior_good_snapshot(indicator_id: str, today: date) -> dict | None:
    """Most-recent good snapshot strictly BEFORE `today` (by scraped_at date).

    Unlike _load_last_good_snapshot, this excludes today's own snapshot — the
    cumulative guard must compare today's value against a genuinely prior value.
    """
    d = DATA_DIR / indicator_id
    if not d.exists():
        return None
    for path in sorted(d.glob("*.json"), reverse=True):
        try:
            blob = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if _is_bad_snapshot(blob):
            continue
        try:
            scraped = datetime.fromisoformat(
                blob["scraped_at"].replace("Z", "+00:00")
            ).date()
        except (KeyError, ValueError):
            continue
        if scraped < today:
            return blob
    return None


def _is_fresh(snapshot: dict, now: datetime) -> bool:
    """Return True if the snapshot is within its cadence staleness threshold."""
    cadence = snapshot.get("cadence", "daily")
    threshold = STALE_THRESHOLDS_HOURS_BY_CADENCE.get(cadence, 24.0)
    try:
        scraped_at = datetime.fromisoformat(snapshot["scraped_at"].replace("Z", "+00:00"))
        if scraped_at.tzinfo is None:
            scraped_at = scraped_at.replace(tzinfo=timezone.utc)
        age_hours = (now - scraped_at).total_seconds() / 3600.0
        return age_hours <= threshold
    except (KeyError, ValueError):
        return False


def _fiscal_year(d: date) -> int:
    """Bangladesh fiscal year (July–June). Returns the FY-start calendar year."""
    return d.year if d.month >= FISCAL_YEAR_START_MONTH else d.year - 1


def _is_cumulative_regression(
    today_value: object,
    prior_value: object,
    today_date: date,
    prior_date: date,
) -> bool:
    """True if a cumulative (FYTD) figure dropped implausibly within the same FY.

    A cumulative fiscal-year-to-date total can only rise within a fiscal year.
    A drop beyond CUMULATIVE_DROP_TOLERANCE in the SAME fiscal year is a parse
    error. A drop across the July FY boundary is the legitimate annual reset.
    """
    if not isinstance(today_value, (int, float)) or isinstance(today_value, bool):
        return False
    if not isinstance(prior_value, (int, float)) or isinstance(prior_value, bool):
        return False
    if prior_value <= 0:
        return False
    if _fiscal_year(today_date) != _fiscal_year(prior_date):
        return False  # FY reset — drop is legitimate
    return today_value < prior_value * (1 - CUMULATIVE_DROP_TOLERANCE)


def _quarantine_flagged(
    data: dict[str, Any],
    flagged_ids: list[str],
    history: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], bool]:
    """Quarantine Opus-flagged fields instead of rejecting the whole snapshot.

    Returns (cleaned_data, quarantined_ids, hard_reject).
    hard_reject is True when the verdict is untrustworthy or too broad:
      * any flagged id is not present in `data`, or
      * more than MAX_QUARANTINE_FIELDS ids are flagged.
    Otherwise each flagged id is replaced with its most-recent good value from
    `history` (newest-last list of archived `.data` dicts); if no historical
    value exists, the field is dropped.
    """
    present = [fid for fid in flagged_ids if fid in data]
    if len(present) != len(flagged_ids):
        return data, [], True   # unmappable flagged id ⇒ don't trust the verdict
    if len(present) > MAX_QUARANTINE_FIELDS:
        return data, [], True   # too broadly broken to publish

    cleaned = dict(data)
    quarantined: list[str] = []
    for fid in present:
        last_good = None
        for snap in reversed(history):  # newest-last ⇒ reversed = newest-first
            v = (snap.get("data") or {}).get(fid)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                last_good = v
                break
        if last_good is not None:
            cleaned[fid] = last_good
        else:
            cleaned.pop(fid, None)
        quarantined.append(fid)
    return cleaned, quarantined, False


def _compute_reserve_utilisation(data_additions: dict[str, Any]) -> None:
    """Mint derived CRR/SLR utilisation ratios into ``data_additions`` in place.

    S2: the Liquidity panel wants CRR/SLR utilisation %, but EconDelta scrapes
    only the LEVELS (``deposits_held_with_bb_crr``, ``excess_liquid_asset_total_minimum``,
    ``deposits_of_the_system``) — there is no scraped maintenance-% cell. So we
    compute the ratio here, after the snapshot loop has populated the level
    scalars and BEFORE the Supabase writer's scalar-only filter, so each ratio
    lands in ``metric_history`` under its own id.

    Each ratio = numerator / denominator × 100, expressed as a % of total system
    deposits — labelled by what it actually divides (no hardcoded statutory CRR/SLR
    rate, which would be a shifting policy constant). Null-safe and idempotent:

      * a missing/non-numeric numerator or denominator → skip (no key written),
        so a missing month renders as a missing metric rather than a bogus 9999%;
      * a zero (or non-positive) denominator → skip (no divide-by-zero);
      * a derived id already present in ``data_additions`` is left untouched.
    """
    for derived_id, (numerator_id, denominator_id) in RESERVE_UTIL_DERIVED.items():
        if derived_id in data_additions:
            continue
        numerator = data_additions.get(numerator_id)
        denominator = data_additions.get(denominator_id)
        if not isinstance(numerator, (int, float)) or isinstance(numerator, bool):
            continue
        if not isinstance(denominator, (int, float)) or isinstance(denominator, bool):
            continue
        if denominator <= 0:
            continue
        data_additions[derived_id] = round(numerator / denominator * 100, 4)


def _build_v3_blocks(
    now: datetime,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], FreshnessSummary, list[Alert]]:
    """Build the v3 data additions, domains, freshness summary, and alerts.

    Returns:
        data_additions: flat {indicator_id: value} dict to merge into data
        domains:        nested {domain: {indicator_id: snapshot}} dict
        freshness:      FreshnessSummary with per-cadence counts
        alerts:         list of Alert objects for anomalous indicators
    """
    registry = _load_v3_registry()
    data_additions: dict[str, Any] = {}
    domains: dict[str, dict[str, Any]] = {}
    cadence_buckets: dict[str, dict] = {}
    indicators_total = 0
    indicators_fresh = 0
    indicators_stale = 0
    indicators_failed = 0
    alerts: list[Alert] = []

    for ind in registry:
        indicator_id = ind["id"]
        domain = ind.get("domain", "macro")
        cadence = ind.get("cadence", "daily")
        cadence_buckets.setdefault(cadence, {"fresh": 0, "expected": 0, "stale_ids": []})
        cadence_buckets[cadence]["expected"] += 1
        indicators_total += 1

        snapshot = _load_v3_snapshot(indicator_id)
        if snapshot is None:
            indicators_failed += 1
            continue

        # Stale-fallback: if today's snapshot is bad (parser wrote 0.0 with
        # provenance=needs_review), walk back through history for the most
        # recent successful extraction and use THAT instead, marked stale.
        # If no good historical snapshot exists, skip the indicator entirely
        # — better the brief shows a missing key than a misleading 0.0.
        if _is_bad_snapshot(snapshot):
            indicators_failed += 1
            historical = _load_last_good_snapshot(indicator_id)
            if historical is None:
                logger.info(
                    "skipping %s — today bad and no good historical snapshot in last 60 days",
                    indicator_id,
                )
                continue
            logger.info(
                "stale-fallback for %s: using %s (today is needs_review)",
                indicator_id,
                historical.get("_stale_from", "?"),
            )
            snapshot = historical

        # Cumulative-monotonicity guard: a FYTD/cumulative total can't fall within
        # a fiscal year. If it did (parser/LLM mis-read), fall back to the prior
        # good value, marked stale — see docs/.../nbr-guard-granular-reject.
        elif ind.get("cumulative"):
            prior = _prior_good_snapshot(indicator_id, now.date())
            if prior is not None:
                try:
                    prior_date = datetime.fromisoformat(
                        prior["scraped_at"].replace("Z", "+00:00")
                    ).date()
                except (KeyError, ValueError):
                    prior_date = None
                if prior_date is not None and _is_cumulative_regression(
                    snapshot.get("value"), prior.get("value"), now.date(), prior_date
                ):
                    logger.error(
                        "cumulative regression for %s: today=%s < prior-good=%s (same FY) "
                        "— stale-fallback to %s",
                        indicator_id, snapshot.get("value"), prior.get("value"),
                        prior.get("scraped_at", "?"),
                    )
                    indicators_failed += 1
                    prior = {**prior, "_provenance": "stale_fallback",
                             "_stale_from": prior.get("scraped_at")}
                    snapshot = prior

        fresh = _is_fresh(snapshot, now) and snapshot.get("_provenance") != "stale_fallback"
        if fresh:
            indicators_fresh += 1
            cadence_buckets[cadence]["fresh"] += 1
        else:
            indicators_stale += 1
            cadence_buckets[cadence]["stale_ids"].append(indicator_id)

        # Add to flat data dict (for The Brief — opportunistic read with no code changes)
        value = snapshot.get("value")
        if isinstance(value, (int, float, str, dict)):
            data_additions[indicator_id] = value

        # Add to domains block grouped by domain
        domains.setdefault(domain, {})[indicator_id] = snapshot

        # Anomaly detection: alert when change_pct exceeds the per-indicator threshold
        change_pct = snapshot.get("change_pct")
        threshold = ind.get("anomaly_threshold")
        if change_pct is not None and threshold is not None and abs(change_pct) >= threshold:
            alerts.append(
                Alert(
                    indicator_id=indicator_id,
                    type="anomaly",
                    severity="warn",
                    value=snapshot.get("value"),
                    previous=snapshot.get("previous_value"),
                    change_pct=change_pct,
                )
            )

    # Derived reserve-utilisation ratios (S2): minted from the level scalars
    # loaded above, BEFORE the writer's scalar-only filter, so they persist to
    # metric_history under their own ids. Null/zero-denominator safe.
    _compute_reserve_utilisation(data_additions)

    freshness = FreshnessSummary(
        indicators_total=indicators_total,
        indicators_fresh=indicators_fresh,
        indicators_stale=indicators_stale,
        indicators_failed=indicators_failed,
        by_cadence={
            c: FreshnessByCadence(
                fresh=v["fresh"],
                expected=v["expected"],
                stale_ids=v["stale_ids"],
            )
            for c, v in cadence_buckets.items()
        },
    )
    return data_additions, domains, freshness, alerts


def _build_source_as_of_map(domains: dict[str, dict[str, Any]]) -> dict[str, date]:
    """Extract per-metric publication dates from the v3 domains snapshot dict.

    Each v3 snapshot written by ``parsers/hybrid.py:_build_snapshot`` may carry
    a ``source_as_of`` string (ISO date, e.g. "2025-09-30") when the parser could
    recover the true publication date from the source document. This function
    collects those dates and returns a metric_id → date mapping that
    ``upsert_metric_history`` uses to override the global run-date ``as_of``.

    Metrics without a ``source_as_of`` key (daily scrapers, fallback runs) are
    simply absent from the returned dict — the writer falls back to today.

    Malformed or missing date strings are silently skipped (logged at DEBUG).
    """
    result: dict[str, date] = {}
    for _domain, indicators in domains.items():
        for indicator_id, snapshot in indicators.items():
            raw = snapshot.get("source_as_of")
            if not raw:
                continue
            try:
                result[indicator_id] = date.fromisoformat(str(raw)[:10])
            except (ValueError, TypeError):
                logger.debug(
                    "skipping malformed source_as_of=%r for %s", raw, indicator_id
                )
    return result


# EconDelta indicator-id ↔ brief metric_id alias map. The brief expects a
# specific naming convention per section (`macro_*`, `remit_*`, `fiscal_*`,
# `banking_*`, `food_*`); EconDelta keeps its own indicator IDs authoritative.
# Pure 1:1 aliases (no unit conversion) live here.
BRIEF_ALIASES: dict[str, str] = {
    # macro
    "macro_cpi_food":      "food_inflation",
    "macro_cpi_headline":  "general_inflation",
    "macro_cpi_nonfood":   "non_food_inflation",
    # YoY % credit growth — Phase 3.3: dedicated scrape from BB MEI bulletin
    # (private_sector_credit_yoy_pct), not derived from the absolute
    # private_sector_credit BDT-crore value.
    "macro_credit_growth": "private_sector_credit_yoy_pct",
    # remittance — bn→mn unit conversion is in BRIEF_CONVERSIONS below.
    # fiscal — crore→trillion conversions are in BRIEF_CONVERSIONS below.
    # NBR FYTD canonical: tax_revenue from the BB PDF (deterministic parse,
    # 5% anomaly threshold). News corroborators (nbr_fytd_collected_tbs,
    # nbr_fytd_collected_dailystar) retired 2026-05-25 — both tag-listing
    # pages drifted onto articles covering different fiscal-year windows,
    # so the cross-check flapped.
    "nbr_fytd_collected_cr":    "tax_revenue",
    # banking primitives
    "banking_broad_money":      "broad_money",
    "banking_reserve_money":    "reserve_money",
    "banking_money_multiplier": "money_multiplier",
    "banking_excess_liquid":    "excess_liquid_asset_total_minimum",
    "banking_deposits":         "deposits_of_the_system",
    "banking_call_money_rate":  "call_money_rate",
    # banking ratios (FSAR — quarterly)
    "banking_npl_pct":          "gross_npl_ratio",
    "banking_car_pct":          "banking_sector_crar",
    # money market — yield headline (daily)
    "tbill_91d_yield_pct":      "bill_bond_rates",
    "gsec_next_auction_cr":     "gsec_auction",
    # money market — brief metric_id forms (the brief's tbond builder
    # uses ``tbond_tbill_91d``; brief's nbr/dam builders use ``dam_*``)
    "tbond_tbill_91d":          "bill_bond_rates",
    # multi-tenor T-Bill / T-Bond yields — feed §07 yield curve chart
    "tbond_tbill_182d":         "tbill_182d_yield",
    "tbond_tbill_364d":         "tbill_364d_yield",
    "tbond_bond_5y":            "tbond_5y_yield",
    "tbond_bond_10y":           "tbond_10y_yield",
    # DAM retail food prices (daily, BDT/kg or BDT/4-pcs for eggs)
    "food_rice_coarse_bdt":     "food_rice_coarse",
    "food_atta_packet_bdt":     "food_atta_packet",
    "food_egg_red_bdt":         "food_egg_red",
    "food_chicken_farm_bdt":    "food_chicken_farm",
    "food_oil_soybean_bdt":     "food_oil_soybean",
    "food_onion_local_bdt":     "food_onion_local",
    "food_lentil_moong_bdt":    "food_lentil_moong",
    "food_sugar_local_bdt":     "food_sugar_local",
    # DAM retail food prices — brief metric_id forms (`dam_*`)
    "dam_rice_coarse":          "food_rice_coarse",
    "dam_lentil":               "food_lentil_moong",
    "dam_oil":                  "food_oil_soybean",
    "dam_sugar":                "food_sugar_local",
    "dam_onion":                "food_onion_local",
    "dam_egg":                  "food_egg_red",
    "dam_chicken":              "food_chicken_farm",
    "dam_flour":                "food_atta_packet",
}

# Aliases that need a unit conversion (source unit → brief unit).
# Format: brief_key → (source_key, multiplier).
BRIEF_CONVERSIONS: dict[str, tuple[str, float]] = {
    # T-Bill / T-Bond outstanding: gsom reports BDT million; brief expects
    # BDT crore (1 crore = 10 million → multiplier 0.1).
    "tbill_outstanding_cr": ("treasury_bill_outstanding", 0.1),
    "tbond_outstanding_cr": ("treasury_bond_outstanding", 0.1),
    # Fiscal: EconDelta indicators are BDT crore, brief renders BDT trillion.
    # 1 trillion BDT = 100,000 crore → multiplier 0.00001.
    "fiscal_nbr_collected_trn":  ("tax_revenue", 0.00001),
    "fiscal_govt_borrow_trn":    ("domestic_borrowing_for_budget_deficit", 0.00001),
    "fiscal_foreign_borrow_trn": ("foreign_borrowing_for_budget_deficit", 0.00001),
    "fiscal_bank_borrow_trn":    ("bank_borrowing_for_deficit_financing", 0.00001),
    "fiscal_nsc_outstanding":    ("nsc_outstanding", 0.00001),
    # Remittance: EconDelta source is USD billion, brief renders USD million.
    # 1 billion = 1,000 million → multiplier 1000.
    "remit_monthly_mn": ("monthly_remittance", 1000.0),
    "remit_fy_mn":      ("fy_remittance", 1000.0),
    # NBR component decomposition (Phase 3.2): articles report BDT crore,
    # brief's §12 expects BDT bn. 1 bn = 100 crore → multiplier 0.01.
    "nbr_vat_bn":       ("nbr_vat_collected_cr", 0.01),
    "nbr_it_bn":        ("nbr_it_collected_cr", 0.01),
    "nbr_customs_bn":   ("nbr_customs_collected_cr", 0.01),
}


def _flatten_dict_indicators(data: dict) -> None:
    """Explode dict-shaped indicator values into per-key numeric entries.

    Phase 3.1: ``dse_sector_heat`` arrives as ``{Banks: -1.4, NBFI: -1.1, ...}``
    from the parser, but Supabase ``metric_history`` only persists numerics
    (the writer filters dicts/strings). We mint one numeric key per sector
    so each lands in Supabase and the brief can read them via the standard
    history path. Idempotent: per-sector keys already in `data` are left
    alone.

    Same treatment for ``call_money_rate``: the parser returns a 4-tenor
    dict ``{1D, 7D, 14D, 90D}``; we fan it out to per-tenor numeric keys
    (``call_money_rate_1d``, ``_7d``, ``_14d``, ``_90d``) AND promote the
    1D (overnight) value to the scalar ``call_money_rate`` itself — BB
    convention: "call money rate" without modifier means overnight. The
    promotion replaces the dict in place so the Supabase writer's
    scalar-only filter persists the headline rate, which in turn makes
    the existing ``BRIEF_ALIASES["banking_call_money_rate"] = "call_money_rate"``
    mapping start working.
    """
    sector_heat = data.get("dse_sector_heat")
    if isinstance(sector_heat, dict):
        for sector, pct in sector_heat.items():
            if not isinstance(pct, (int, float)):
                continue
            key = "dse_sector_heat_" + str(sector).lower().replace(" ", "_")
            if key not in data:
                data[key] = float(pct)

    call_money = data.get("call_money_rate")
    if isinstance(call_money, dict):
        for tenor, rate in call_money.items():
            if not isinstance(rate, (int, float)):
                continue
            key = "call_money_rate_" + str(tenor).lower()
            if key not in data:
                data[key] = float(rate)
        overnight = call_money.get("1D")
        if isinstance(overnight, (int, float)):
            # Mutate dict → scalar so the Supabase writer (scalars only)
            # persists the headline overnight rate as ``call_money_rate``.
            data["call_money_rate"] = float(overnight)

    _flatten_ownership_cluster(
        data,
        source_key="npl_by_ownership",
        key_prefix="npl_",
        key_suffix="_pct",
    )
    _flatten_ownership_cluster(
        data,
        source_key="deposits_by_ownership",
        key_prefix="deposits_",
        key_suffix="_cr",
    )


def _flatten_ownership_cluster(
    data: dict, *, source_key: str, key_prefix: str, key_suffix: str
) -> None:
    """Explode a 4-way bank-ownership cluster dict into per-segment scalars (S10).

    The ``pdf_fsr_ownership_cluster`` parser returns a dict keyed by the four
    canonical ownership segments — ``{"socb": .., "pcb": .., "fcb": ..,
    "specialised": ..}`` — for two FSR clusters:

      - ``npl_by_ownership``      → ``npl_socb_pct`` / ``npl_pcb_pct`` /
                                    ``npl_fcb_pct`` / ``npl_specialised_pct``
                                    (per-segment NPL ratio, percent).
      - ``deposits_by_ownership`` → ``deposits_socb_cr`` / ``deposits_pcb_cr`` /
                                    ``deposits_fcb_cr`` / ``deposits_specialised_cr``
                                    (per-segment deposit LEVEL, BDT crore — NOT
                                    a share; the donut computes shares downstream
                                    so they stay consistent with
                                    ``deposits_of_the_system``).

    Mirrors the ``call_money_rate`` / ``dse_sector_heat`` fan-out: we mint one
    numeric key per segment BEFORE the Supabase writer's scalar-only filter
    drops the dict (landmine C). Idempotent: a per-segment key already in
    ``data`` is left alone. No-op when the cluster indicator is absent or the
    value isn't a dict.
    """
    cluster = data.get(source_key)
    if not isinstance(cluster, dict):
        return
    for segment, value in cluster.items():
        if not isinstance(value, (int, float)):
            continue
        key = f"{key_prefix}{str(segment).lower()}{key_suffix}"
        if key not in data:
            data[key] = float(value)


def _apply_brief_aliases(data: dict) -> None:
    """Mutate `data` in place: surface EconDelta keys under brief-key names
    and apply unit conversions. Idempotent: if a brief_key already exists
    it's left untouched (so a hand-set value upstream wins).
    """
    _flatten_dict_indicators(data)

    for brief_key, econdelta_key in BRIEF_ALIASES.items():
        if econdelta_key in data and brief_key not in data:
            data[brief_key] = data[econdelta_key]

    for brief_key, (source_key, mult) in BRIEF_CONVERSIONS.items():
        if source_key in data and brief_key not in data:
            v = data[source_key]
            if isinstance(v, (int, float)):
                data[brief_key] = round(v * mult, 2)

    if "nbr_fytd_collected_cr" in data and "nbr_fytd_cross_check" not in data:
        data["nbr_fytd_cross_check"] = "single_source_tax_revenue"


def _titleize(metric_id: str) -> str:
    """Convert 'banking_npl_pct' -> 'Banking Npl Pct'."""
    return " ".join(word.capitalize() for word in metric_id.split("_"))


# metric_definitions rows for runtime-derived metrics (no sources-v3.json
# config entry — they have no fetch). `_build_definition_seeds` appends these
# so the catalog/Supabase definitions stay in sync with the values minted in
# `_build_v3_blocks`. Keyed by metric_id for idempotent merging.
DERIVED_DEFINITION_SEEDS: list[dict] = [
    {
        "metric_id": "crr_utilisation_pct",
        "label": "CRR balance as % of system deposits",
        "short_label": None,
        "unit": "%",
        "domain": "monetary_aggregates",
        "cadence": "monthly",
        "description": (
            "Derived (S2): deposits_held_with_bb_crr / deposits_of_the_system × 100. "
            "CRR balance held with Bangladesh Bank expressed as a % of total system "
            "deposits — NOT the regulated statutory maintenance ratio (no hardcoded "
            "policy rate). Computed in aggregate_latest._compute_reserve_utilisation."
        ),
        "source": "BB MEI (derived)",
        "source_url": None,
    },
    {
        "metric_id": "slr_utilisation_pct",
        "label": "Excess liquid assets as % of system deposits",
        "short_label": None,
        "unit": "%",
        "domain": "monetary_aggregates",
        "cadence": "monthly",
        "description": (
            "Derived (S2): excess_liquid_asset_total_minimum / deposits_of_the_system "
            "× 100. Excess liquid assets held over the statutory SLR minimum, expressed "
            "as a % of total system deposits — NOT the regulated maintenance ratio. "
            "Computed in aggregate_latest._compute_reserve_utilisation."
        ),
        "source": "BB MEI (derived)",
        "source_url": None,
    },
]


def _build_definition_seeds(sources_v3_cfg: dict) -> list[dict]:
    """Build metric_definitions rows from sources-v3.json indicators.

    Conservative defaults: label falls back to titleized id, sort_order=100,
    is_hero=False. Tunable in Supabase Studio post-insert.

    Runtime-derived metrics (CRR/SLR utilisation — minted in `_build_v3_blocks`,
    no config entry) are appended from ``DERIVED_DEFINITION_SEEDS`` so their
    Supabase definitions stay in sync with the values that land in metric_history.
    Idempotent on metric_id: a derived id already produced from config wins.
    """
    seeds = []
    seen_ids: set[str] = set()
    for ind in sources_v3_cfg.get("indicators", []):
        seeds.append({
            "metric_id": ind["id"],
            "label": ind.get("label") or _titleize(ind["id"]),
            "short_label": ind.get("short_label"),
            "unit": ind.get("unit"),
            "domain": ind.get("domain", "Other"),
            "cadence": ind.get("cadence"),
            "description": ind.get("description"),
            "source": ind.get("source"),
            "source_url": (ind.get("fetch") or {}).get("url"),
        })
        seen_ids.add(ind["id"])

    for derived in DERIVED_DEFINITION_SEEDS:
        if derived["metric_id"] not in seen_ids:
            seeds.append(dict(derived))

    return seeds


def write_latest(bundle: LatestBundle) -> None:
    """Atomic write: .tmp -> os.replace."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = LATEST_PATH.with_suffix(".json.tmp")
    payload = bundle.model_dump(mode="json")
    with tmp_path.open("w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp_path, LATEST_PATH)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    now = datetime.now(timezone.utc)

    with CONFIG_PATH.open() as f:
        sources_cfg = json.load(f)["sources"]

    snapshots: dict[str, Any] = {}
    sources_status: dict[str, SourceStatus] = {}

    for key, (subdir_name, schema_class, url_key) in SCRAPER_SPEC.items():
        subdir = DATA_DIR / subdir_name
        latest_file = find_latest_snapshot(subdir)
        snapshot = load_snapshot(latest_file, schema_class) if latest_file else None
        snapshots[key] = snapshot
        url = sources_cfg.get(url_key, {}).get("url") if url_key else None
        sources_status[key] = compute_status(snapshot, url, now)

    data = flatten_data(snapshots)

    # v3 expansion: registry-driven domain blocks, freshness, alerts;
    # v3 indicator values also land in the flat `data` dict for The Brief.
    data_additions, domains, freshness, alerts = _build_v3_blocks(now)
    data.update(data_additions)

    # Forex-source aliases AFTER the v3 merge: the parse-stage versions of these
    # indicators come from BB PDFs and frequently fail (Akamai TSPD challenge,
    # PDF format drift) — leaving 0.0 in data_additions which would shadow the
    # working bb_forex.py-direct scrape. Apply the alias here so it wins.
    forex = snapshots.get("bb_forex")
    if forex is not None:
        data["usd_bdt_exchange_rate"] = forex.rates.usd_bdt_mid
        if forex.reserves is not None:
            data["fx_reserve_gross_and_bpm6"] = forex.reserves.gross_reserves_usd_bn

    _apply_brief_aliases(data)

    try:
        bundle = LatestBundle(
            schema_version="3.0",
            updated_at=now,
            sources_status=sources_status,
            data=data,
            domains=domains,
            freshness=freshness,
            alerts=alerts,
        )
    except ValidationError as e:
        logger.exception("bundle validation failed")
        notify("error", "aggregator validation failed", str(e))
        return 1

    # Opus 4.6 high-effort review: compare proposed `data` against the last 5 days
    # of archived latest.json. If reject, exit 1 without overwriting — the existing
    # latest.json (yesterday's last good run) becomes the rollback. The systemd
    # retry timers will give Step 1+2 a second pass; if they still produce a reject,
    # the brief publishes against yesterday's data with stale-section markers.
    if os.environ.get("ECONDELTA_SKIP_OPUS_REVIEW") == "1":
        logger.info("ECONDELTA_SKIP_OPUS_REVIEW=1 — skipping Opus review")
    else:
        history = load_history(ARCHIVE_DIR, days=5)
        if not history:
            logger.info("no archive history yet — skipping Opus review on this run")
        else:
            verdict = review_data(data, history)
            status = verdict.get("status", "ok")
            reason = verdict.get("reason", "")
            if verdict.get("skipped"):
                logger.info("opus review skipped: %s", reason)
            elif status == "reject":
                missing = verdict.get("missing", []) or []
                anomalies = verdict.get("anomalies", []) or []
                flagged = [a.get("indicator") for a in anomalies if a.get("indicator")]
                flagged = list({*flagged, *missing})
                cleaned, quarantined, hard_reject = _quarantine_flagged(data, flagged, history)
                if hard_reject:
                    logger.error(
                        "opus review REJECTED (hard): %s | missing=%s | anomalies=%d "
                        "(unmappable or >%d fields) — keeping yesterday's latest.json",
                        reason, missing[:5], len(anomalies), MAX_QUARANTINE_FIELDS,
                    )
                    notify(
                        "warn",
                        "EconDelta Opus review rejected today's data",
                        f"reason: {reason}\nmissing: {missing[:5]}\nanomalies: {len(anomalies)}\n"
                        f"keeping yesterday's latest.json — retry timers will re-run.",
                    )
                    return 1
                # Granular path: quarantine the flagged fields, publish the rest.
                logger.warning(
                    "opus review reject → quarantined %d field(s): %s | reason: %s",
                    len(quarantined), quarantined, reason,
                )
                notify(
                    "warn",
                    "EconDelta published with fields quarantined",
                    f"reason: {reason}\nquarantined: {quarantined}\n"
                    f"these fields use last-good values; the rest published fresh.",
                )
                data = cleaned
                bundle = LatestBundle(
                    schema_version="3.0",
                    updated_at=now,
                    sources_status=sources_status,
                    data=data,
                    domains=domains,
                    freshness=freshness,
                    alerts=alerts,
                )
            else:
                logger.info("opus review OK: %s (confidence=%s)", reason, verdict.get("confidence"))

    write_latest(bundle)
    # Archive a daily copy for tomorrow's Opus review. Same-day runs overwrite,
    # so the LAST successful aggregate of the day is what tomorrow compares against.
    archived = archive_latest(LATEST_PATH, ARCHIVE_DIR)
    if archived is not None:
        logger.info("archived to %s", archived.name)

    # Seed metric_definitions for any new indicators (idempotent).
    from utils.supabase_writer import upsert_metric_definitions_seed
    sources_v3 = json.loads(SOURCES_V3_PATH.read_text()) if SOURCES_V3_PATH.exists() else {"indicators": []}
    seeds = _build_definition_seeds(sources_v3)
    inserted = upsert_metric_definitions_seed(seeds)
    if inserted:
        logger.info("Seeded %d new metric_definitions rows", inserted)

    # Persist to Supabase metric_history (warm queryable history). Best-effort:
    # local archive (above) is the cold backup, and the next aggregate retry
    # idempotently re-upserts the same (metric_id, as_of) rows. ECONDELTA_SKIP_SUPABASE=1
    # disables the call (set in tests/conftest.py and any dev runs).
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") != "1":
        try:
            from utils.supabase_writer import (
                SupabaseWriteError,
                upsert_metric_history,
            )
            # Build per-metric publication-date overrides from v3 snapshot metadata.
            # Slow-cadence metrics (quarterly FSAR, monthly news) carry source_as_of
            # from the parser so metric_history.as_of reflects the true publication
            # date rather than today's run date — fixing the freshness-pill lie.
            source_as_of_map = _build_source_as_of_map(domains)
            n_rows = upsert_metric_history(
                data=data, as_of=now.date(), source_as_of_map=source_as_of_map,
            )
            logger.info(
                "upserted %d rows to Supabase metric_history (as_of=%s, overrides=%d)",
                n_rows, now.date(), len(source_as_of_map),
            )
        except SupabaseWriteError as e:
            logger.warning(
                "Supabase write failed: %s — continuing with local archive only", e,
            )

    summary = " ".join(
        f"{k}={s.status}({s.age_hours}h)" if s.age_hours is not None else f"{k}={s.status}"
        for k, s in sources_status.items()
    )
    logger.info("wrote %s -- %s", LATEST_PATH, summary)

    bad = {k: s.status for k, s in sources_status.items() if s.status != "ok"}
    if bad:
        notify(
            "warning",
            "aggregator -- sources not OK",
            "\n".join(f"{k}: {v}" for k, v in bad.items()),
        )

    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("aggregate", "econdelta-aggregate.service", main))
