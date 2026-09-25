"""Local source-poll receipts and revision diffs, separate from persistence success.

A source checked today can still publish an old month. These receipts keep that
clock distinct, retain accepted historical values and expose proposed revisions
for R1/operator review. They never write to Supabase.
"""
from __future__ import annotations

import calendar
import json
import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_DIRECTORY = Path(__file__).resolve().parent.parent / "data/monthly_evidence"
logger = logging.getLogger(__name__)


def revision_diff(candidates: list[dict], existing: list[dict]) -> list[dict]:
    """Only compare the same metric and month; never apply a source revision."""
    stored = {(r.get("metric_id"), str(r.get("as_of"))[:10]): r for r in existing}
    differences = []
    for row in candidates:
        old = stored.get((row["metric_id"], row["as_of"][:10]))
        if old is None:
            continue
        try:
            prior, current = float(old["value"]), float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(prior) and math.isfinite(current) and prior != current:
            differences.append({"metric_id": row["metric_id"], "as_of": row["as_of"],
                                "stored_value": prior, "source_value": current,
                                "stored_source": old.get("source"), "source": row.get("source")})
    return differences


def record_source_check(
    metric_id: str, parsed: list[tuple[date, float]], existing: list[dict], *,
    today: date, revisions: list[dict], source_url: str, directory: Path | None = None,
) -> dict:
    """Persist a dated source receipt; old observation periods remain old."""
    latest = max((day for day, _ in parsed if day < today.replace(day=1)), default=None)
    period = latest.replace(day=calendar.monthrange(latest.year, latest.month)[1]) if latest else None
    previous_end = today.replace(day=1).toordinal() - 1
    report = {
        "metric_id": metric_id, "checked_at": datetime.now(timezone.utc).isoformat(),
        "latest_source_period": period.isoformat() if period else None,
        "latest_stored_key": max((str(r["as_of"])[:10] for r in existing), default=None),
        "status": "supported" if period and period.toordinal() == previous_end else "release-lag",
        "source_url": source_url, "revisions": revisions,
        "persistence": "not-confirmed-by-source-poll",
    }
    destination = directory or DEFAULT_DIRECTORY
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{metric_id}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(path)
    if revisions:
        logger.warning("Monthly revision review required: %s", json.dumps(revisions))
    logger.info("Monthly source check: %s", json.dumps(report))
    return report

# Actual Brief monthly archive readers (19): chart tuples plus macro archive_id,
# not display names of derived or daily-read metrics. The fiscal chart requests
# nbr_revenue_monthly_cr (28 backup rows through October 2025; manual writer).
BRIEF_MONTHLY_IDS = frozenset({
    "cpi_12m_avg_monthly", "cpi_p2p_food_monthly", "cpi_p2p_nonfood_monthly",
    "exports_usd_mn_monthly", "imports_usd_mn_monthly", "remittance_usd_mn_monthly",
    "gross_reserves_usd_bn_monthly", "net_reserves_bpm6_usd_bn_monthly",
    "tbill_91d_yield_monthly", "tbill_182d_yield_monthly", "tbill_364d_yield_monthly",
    "yield_2y_monthly", "yield_5y_monthly", "yield_10y_monthly", "yield_15y_monthly",
    "yield_20y_monthly", "nbr_revenue_monthly_cr", "reer_monthly", "m2_growth_yoy_monthly",
})
PARKED_DISPOSITIONS = {
    "reer_monthly": "unsupported",
    "nbr_revenue_monthly_cr": "unsupported",
    "non_nbr_tax_revenue": "owner-blocked",
    "non_tax_revenue": "owner-blocked",
}
POLLED_IDS = frozenset({"exports_usd_mn_monthly", "imports_usd_mn_monthly",
                        "remittance_usd_mn_monthly", "m2_growth_yoy_monthly",
                        "cpi_12m_avg_monthly", "cpi_p2p_food_monthly", "cpi_p2p_nonfood_monthly"})


def source_monitor(*, directory: Path | None = None, now: datetime | None = None) -> dict:
    """Expose source status independently of poll liveness; never waive freshness.

    Non-polled ladder/reserves legs use their own service run logs. No receipt is
    claimed for them. A skipped caught-up browser fetch likewise ages as a poll,
    not as proof the job died. 26h matches the existing off-box job check window.
    """
    now = now or datetime.now(timezone.utc)
    root = directory or DEFAULT_DIRECTORY
    result = {}
    for mid in sorted(BRIEF_MONTHLY_IDS | PARKED_DISPOSITIONS.keys()):
        row = {"source_status": PARKED_DISPOSITIONS.get(mid, "supported"),
               "job_status": "separate-service-run-log", "latest_source_period": None}
        if mid in POLLED_IDS:
            row["job_status"] = "not-checked"
            row["source_status"] = "unknown"
            try:
                receipt = json.loads((root / f"{mid}.json").read_text())
                checked = datetime.fromisoformat(receipt["checked_at"].replace("Z", "+00:00"))
                age = (now - checked).total_seconds()
                row.update(source_status=receipt["status"],
                           latest_source_period=receipt["latest_source_period"],
                           job_status="checked" if 0 <= age <= 26 * 3600 else "not-recently-checked")
            except (OSError, ValueError, KeyError, TypeError):
                pass  # explicit unknown, never "all current"
        result[mid] = row
    return result


def audit_candidates(candidates: list[dict], existing: list[dict], *, today: date,
                     source_url: str) -> None:
    """Record validated candidates before the append-only filter; no rewrite."""
    for mid in sorted({row["metric_id"] for row in candidates}):
        selected = [row for row in candidates if row["metric_id"] == mid]
        prior = [{**row, "metric_id": mid} for row in existing
                 if row.get("metric_id", mid) == mid]
        parsed = [(date.fromisoformat(row["as_of"][:10]), row["value"]) for row in selected]
        try:
            record_source_check(mid, parsed, prior, today=today,
                                revisions=revision_diff(selected, prior), source_url=source_url)
        except OSError as exc:
            # Local diagnostics must not discard independently validated rows.
            logger.warning("Monthly source receipt failed for %s: %s", mid, exc)
