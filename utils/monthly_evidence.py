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
from typing import Literal

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
            differences.append(
                {
                    "metric_id": row["metric_id"],
                    "as_of": row["as_of"],
                    "stored_value": prior,
                    "source_value": current,
                    "stored_source": old.get("source"),
                    "source": row.get("source"),
                }
            )
    return differences


def record_source_check(
    metric_id: str,
    parsed: list[tuple[date, float]],
    existing: list[dict],
    *,
    today: date,
    revisions: list[dict],
    source_url: str,
    evidence_kind: Literal["upstream-source", "database-observations"],
    directory: Path | None = None,
) -> dict:
    """Persist explicitly typed evidence; a database reread proves no source poll.

    An old period establishes age, not why publication stopped. Even a real
    fetch is labelled observed-older-period rather than asserting release lag.
    """
    latest = max((day for day, _ in parsed if day < today.replace(day=1)), default=None)
    period = (
        latest.replace(day=calendar.monthrange(latest.year, latest.month)[1]) if latest else None
    )
    previous_end = today.replace(day=1).toordinal() - 1
    upstream = evidence_kind == "upstream-source"
    status = "unknown"
    if upstream and period:
        status = "supported" if period.toordinal() == previous_end else "observed-older-period"
    report = {
        "evidence_kind": evidence_kind,
        "metric_id": metric_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "latest_source_period": period.isoformat() if upstream and period else None,
        "latest_database_period": period.isoformat() if not upstream and period else None,
        "latest_stored_key": max((str(r["as_of"])[:10] for r in existing), default=None),
        "status": status,
        "source_url": source_url,
        "revisions": revisions,
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
BRIEF_MONTHLY_IDS = frozenset(
    {
        "cpi_12m_avg_monthly",
        "cpi_p2p_food_monthly",
        "cpi_p2p_nonfood_monthly",
        "exports_usd_mn_monthly",
        "imports_usd_mn_monthly",
        "remittance_usd_mn_monthly",
        "gross_reserves_usd_bn_monthly",
        "net_reserves_bpm6_usd_bn_monthly",
        "tbill_91d_yield_monthly",
        "tbill_182d_yield_monthly",
        "tbill_364d_yield_monthly",
        "yield_2y_monthly",
        "yield_5y_monthly",
        "yield_10y_monthly",
        "yield_15y_monthly",
        "yield_20y_monthly",
        "nbr_revenue_monthly_cr",
        "reer_monthly",
        "m2_growth_yoy_monthly",
    }
)
PARKED_DISPOSITIONS = {
    "reer_monthly": "unsupported",
    "nbr_revenue_monthly_cr": "unsupported",
    "non_nbr_tax_revenue": "owner-blocked",
    "non_tax_revenue": "owner-blocked",
}
POLLED_IDS = frozenset(
    {
        "exports_usd_mn_monthly",
        "imports_usd_mn_monthly",
        "remittance_usd_mn_monthly",
        "m2_growth_yoy_monthly",
        "cpi_12m_avg_monthly",
        "cpi_p2p_food_monthly",
        "cpi_p2p_nonfood_monthly",
    }
)


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
        row = {
            "source_status": PARKED_DISPOSITIONS.get(mid, "supported"),
            "job_status": "separate-service-run-log",
            "latest_source_period": None,
        }
        if mid in POLLED_IDS:
            row["job_status"] = "not-checked"
            row["source_status"] = "unknown"
            try:
                receipt = json.loads((root / f"{mid}.json").read_text())
                checked = datetime.fromisoformat(receipt["checked_at"].replace("Z", "+00:00"))
                age = (now - checked).total_seconds()
                kind = receipt.get("evidence_kind", "unknown")
                row["evidence_kind"] = kind
                if kind == "upstream-source":
                    row.update(
                        source_status=receipt["status"],
                        latest_source_period=receipt["latest_source_period"],
                        job_status="checked" if 0 <= age <= 26 * 3600 else "not-recently-checked",
                    )
                else:
                    # Old untyped receipts may be the database rereads that
                    # motivated this fix. Neither they nor typed database
                    # observations establish that an upstream job ran.
                    row["job_status"] = "unknown"
                    if kind == "database-observations":
                        row["latest_database_period"] = receipt.get("latest_database_period")
                        row["database_check_status"] = (
                            "checked" if 0 <= age <= 26 * 3600 else "not-recently-checked"
                        )
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass  # explicit unknown, never "all current" (and never a crashed aggregate)
        result[mid] = row
    return result


# latest.json sources_status key -> the family's monthly metric ids. Shared with The Brief
# (tests/fixtures/contracts/brief-observations-v1.json, upstream_liveness_contract).
UPSTREAM_FAMILIES: dict[str, tuple[str, ...]] = {
    "cpi_upstream_poll": ("cpi_12m_avg_monthly", "cpi_p2p_food_monthly", "cpi_p2p_nonfood_monthly"),
    "m2_upstream_poll": ("m2_growth_yoy_monthly",),
}
_FAMILY_LABELS = {"cpi_upstream_poll": "CPI", "m2_upstream_poll": "M2"}
# Why a source_monitor row is not liveness -> (SourceStatus status, the reason printed).
_POLL_GAPS = {
    "not-recently-checked": ("stale", "last upstream poll older than the 26-hour job-check window"),
    "database-observations": ("missing", "database reread only, not an upstream source poll"),
    "untyped": ("missing", "untyped receipt, not proof of an upstream poll"),
    "not-checked": ("missing", "no readable source-poll receipt"),
}


def _poll_gap(row: dict) -> tuple[str, str] | None:
    """None only for a typed upstream-source receipt checked within the job window."""
    job = row.get("job_status")
    if job == "checked":
        return None
    if job == "unknown":
        kind = row.get("evidence_kind")
        return _POLL_GAPS["database-observations" if kind == "database-observations" else "untyped"]
    return _POLL_GAPS["not-recently-checked" if job == "not-recently-checked" else "not-checked"]


def _behind_source(vintage: date | None, row: dict) -> tuple[str, str] | None:
    """A live poll is healthy lag only if our own table already holds the period the page
    states (R2 fix H3 review; controller ruling (2)). `row` is the metric's E6 row, which
    carries the monthly leg's database-reread period; months are compared, not days."""
    try:
        recorded = date.fromisoformat(row["latest_database_period"])
    except (KeyError, TypeError, ValueError):
        recorded = None
    if vintage is None or recorded is None:
        return ("missing", "no recorded month to compare the source's period with")
    if (vintage.year, vintage.month) > (recorded.year, recorded.month):
        return (
            "stale",
            "source states a newer period than we have recorded "
            f"(page {vintage.isoformat()}, recorded {recorded.isoformat()})",
        )
    return None


def upstream_poll_status(
    monitor: dict[str, dict],
    polls: dict[str, dict] | None = None,
    now: datetime | None = None,
) -> dict[str, dict]:
    """source_monitor rows -> one SourceStatus-shaped entry per CPI/M2 family.

    `polls` (R2 fix H3, utils.upstream_poll.read_polls) holds the fetch stage's own
    upstream-poll receipts by monthly id; where one exists it decides, otherwise the E6 row
    does. A database reread, an untyped or an unreadable receipt is never "ok"; nor is a live
    poll whose page states a newer month than our own table holds, or one we cannot compare.
    A failed poll outranks missing evidence, which outranks an old poll or a pipeline behind
    its source; every gap names its metric ids.
    `last_success`/`age_hours` are the family's oldest successful poll, known only when every
    member has one.
    """
    polls = polls or {}
    result = {}
    for key, metric_ids in UPSTREAM_FAMILIES.items():
        gaps: dict[tuple[str, str], list[str]] = {}
        for mid in metric_ids:
            if mid in polls:
                gap = polls[mid]["gap"] or _behind_source(
                    polls[mid].get("vintage"), monitor.get(mid, {})
                )
            else:
                gap = _poll_gap(monitor.get(mid, {}))
            if gap is not None:
                gaps.setdefault(gap, []).append(mid)
        states = {state for state, _ in gaps}
        status = next((s for s in ("failed", "missing", "stale") if s in states), "ok")
        detail = "; ".join(f"{phrase} ({', '.join(ids)})" for (_, phrase), ids in gaps.items())
        label = _FAMILY_LABELS[key]
        error = f"{label} upstream source-poll liveness not established: {detail}" if gaps else None
        successes = [polls.get(mid, {}).get("last_success") for mid in metric_ids]
        last = min(successes) if all(successes) else None
        age = round((now - last).total_seconds() / 3600, 2) if last and now else None
        result[key] = {
            "status": status,
            "last_success": last,
            "age_hours": age,
            "url": None,
            "error": error,
        }
    return result


def audit_candidates(
    candidates: list[dict],
    existing: list[dict],
    *,
    today: date,
    source_url: str,
    evidence_kind: Literal["upstream-source", "database-observations"],
) -> None:
    """Record validated candidates before the append-only filter; no rewrite."""
    for mid in sorted({row["metric_id"] for row in candidates}):
        selected = [row for row in candidates if row["metric_id"] == mid]
        prior = [{**row, "metric_id": mid} for row in existing if row.get("metric_id", mid) == mid]
        parsed = [(date.fromisoformat(row["as_of"][:10]), row["value"]) for row in selected]
        try:
            record_source_check(
                mid,
                parsed,
                prior,
                today=today,
                revisions=revision_diff(selected, prior),
                source_url=source_url,
                evidence_kind=evidence_kind,
            )
        except OSError as exc:
            # Local diagnostics must not discard independently validated rows.
            logger.warning("Monthly source receipt failed for %s: %s", mid, exc)
