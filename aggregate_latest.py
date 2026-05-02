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
        # Alias: the parse-stage indicator usd_bdt_exchange_rate is fragile (PDF
        # extraction via Akamai-blocked source); the bb_forex.py direct scrape is
        # the same number. Pre-fill so a parse failure doesn't leave it 0/null.
        data["usd_bdt_exchange_rate"] = forex.rates.usd_bdt_mid
        if forex.reserves is not None:
            data["gross_reserves_usd_bn"] = forex.reserves.gross_reserves_usd_bn
            data["import_cover_months"] = forex.reserves.import_cover_months
            data["reserves_date"] = forex.reserves.reserves_date.isoformat()
            # Same alias rationale: parse-stage fx_reserve_gross_and_bpm6 mirrors
            # what bb_forex.py already fetched cleanly.
            data["fx_reserve_gross_and_bpm6"] = forex.reserves.gross_reserves_usd_bn

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

        provenance = snapshot.get("_provenance")
        if provenance == "needs_review" or snapshot.get("_parse_strategy") == "extract_failed":
            indicators_failed += 1

        fresh = _is_fresh(snapshot, now)
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

    write_latest(bundle)

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
