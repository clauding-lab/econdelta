"""Aggregates latest snapshot from each scraper into data/latest.json — the canonical
file The Brief reads. Atomic write, Pydantic-validated, with per-source status."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
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

logger = logging.getLogger("aggregate_latest")


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

# NBR cross-check tolerance (relative). TBS and Daily Star independently
# report NBR FYTD collection; a >5% gap is treated as a mismatch (likely a
# transcription error or stale article in one source).
NBR_CROSS_CHECK_TOLERANCE = 0.05


def _apply_brief_aliases(data: dict) -> None:
    """Mutate `data` in place: surface EconDelta keys under brief-key names,
    apply unit conversions, and run the NBR cross-check between TBS and
    Daily Star sources. Idempotent: if a brief_key already exists it's left
    untouched (so a hand-set value upstream wins).
    """
    for brief_key, econdelta_key in BRIEF_ALIASES.items():
        if econdelta_key in data and brief_key not in data:
            data[brief_key] = data[econdelta_key]

    for brief_key, (source_key, mult) in BRIEF_CONVERSIONS.items():
        if source_key in data and brief_key not in data:
            v = data[source_key]
            if isinstance(v, (int, float)):
                data[brief_key] = round(v * mult, 2)

    nbr_tbs = data.get("nbr_fytd_collected_tbs")
    nbr_ds = data.get("nbr_fytd_collected_dailystar")
    if "nbr_fytd_collected_cr" in data:
        return
    if isinstance(nbr_tbs, (int, float)) and isinstance(nbr_ds, (int, float)):
        delta = abs(nbr_tbs - nbr_ds) / max(nbr_tbs, nbr_ds)
        if delta <= NBR_CROSS_CHECK_TOLERANCE:
            data["nbr_fytd_collected_cr"] = round((nbr_tbs + nbr_ds) / 2, 2)
            data["nbr_fytd_cross_check"] = "confirmed"
        else:
            # Cumulative collection only grows during a fiscal year; the
            # larger number is more likely to be the later-month report.
            data["nbr_fytd_collected_cr"] = max(nbr_tbs, nbr_ds)
            data["nbr_fytd_cross_check"] = f"mismatch_{delta:.2%}"
            logger.warning(
                "NBR cross-check mismatch: TBS=%s DS=%s delta=%.2f%%",
                nbr_tbs, nbr_ds, delta * 100,
            )
    elif isinstance(nbr_tbs, (int, float)):
        data["nbr_fytd_collected_cr"] = nbr_tbs
        data["nbr_fytd_cross_check"] = "tbs_only"
    elif isinstance(nbr_ds, (int, float)):
        data["nbr_fytd_collected_cr"] = nbr_ds
        data["nbr_fytd_cross_check"] = "dailystar_only"


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
                missing = verdict.get("missing", [])
                anomalies = verdict.get("anomalies", [])
                logger.error(
                    "opus review REJECTED: %s | missing=%s | anomalies=%d",
                    reason, missing[:5], len(anomalies),
                )
                notify(
                    "warn",
                    "EconDelta Opus review rejected today's data",
                    f"reason: {reason}\nmissing: {missing[:5]}\nanomalies: {len(anomalies)}\n"
                    f"keeping yesterday's latest.json — retry timers will re-run; "
                    f"if next aggregate-retry's review also rejects, brief publishes against yesterday's data.",
                )
                return 1
            else:
                logger.info("opus review OK: %s (confidence=%s)", reason, verdict.get("confidence"))

    write_latest(bundle)
    # Archive a daily copy for tomorrow's Opus review. Same-day runs overwrite,
    # so the LAST successful aggregate of the day is what tomorrow compares against.
    archived = archive_latest(LATEST_PATH, ARCHIVE_DIR)
    if archived is not None:
        logger.info("archived to %s", archived.name)

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
            n_rows = upsert_metric_history(data=data, as_of=now.date())
            logger.info("upserted %d rows to Supabase metric_history (as_of=%s)", n_rows, now.date())
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
    sys.exit(main())
