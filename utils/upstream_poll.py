"""Typed receipts for the real upstream CPI/M2 source polls (R2 fix H3).

The daily fetch stage (`fetch_all`, econdelta-fetch.timer, 01:10 BDT) is the only job that
polls Bangladesh Bank's CPI and M2 pages. Each of those polls writes one receipt here, in its
own directory (`data/upstream_polls/<source id>.json`). The monthly legs' database-reread
receipts live in `data/monthly_evidence/<metric_id>.json`, so they can never overwrite one.
The aggregate reads these receipts into fix 5's `cpi_upstream_poll` / `m2_upstream_poll`
sources_status entries. Local files only: nothing here touches Supabase, alerts or the network.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from utils.monthly_evidence import UPSTREAM_FAMILIES

DIRECTORY_NAME = "upstream_polls"
RECEIPT_KIND = "upstream-poll"
PollStatus = Literal["ok", "failed", "unknown"]
_POLL_STATUSES = ("ok", "failed", "unknown")
logger = logging.getLogger(__name__)

# Monthly chart id -> the v3 indicator whose daily fetch is its real upstream poll. Mirrors
# aggregate_latest._CPI_DAILY_TO_MONTHLY and _M2_DAILY_ID/_M2_MONTHLY_ID (a test pins both).
UPSTREAM_POLL_SOURCES: dict[str, str] = {
    "cpi_12m_avg_monthly": "general_inflation",
    "cpi_p2p_food_monthly": "food_inflation",
    "cpi_p2p_nonfood_monthly": "non_food_inflation",
    "m2_growth_yoy_monthly": "m2_growth_yoy_pct",
}
POLLED_SOURCE_IDS = frozenset(UPSTREAM_POLL_SOURCES.values())
# Accepted poll cadence per family. Both are polled by the one daily fetch timer; 26 h is
# E6's existing job-check window for a daily poll (24 h plus 2 h slack for a slow run).
POLL_CADENCE_HOURS: dict[str, int] = {"cpi_upstream_poll": 26, "m2_upstream_poll": 26}
_CADENCE_OF = {
    mid: POLL_CADENCE_HOURS[key] for key, ids in UPSTREAM_FAMILIES.items() for mid in ids
}


def record_upstream_poll(
    source_id: str,
    *,
    status: PollStatus,
    checked_at: datetime,
    source_url: str,
    latest_source_vintage: date | None,
    reason: str | None,
    directory: Path,
) -> dict:
    """Atomically write one poll's receipt, carrying the last successful poll time forward."""
    if checked_at.utcoffset() is None:
        raise ValueError(f"upstream poll of {source_id}: checked_at must be timezone-aware")
    path = directory / f"{source_id}.json"
    checked = checked_at.isoformat()
    receipt = {
        "receipt_kind": RECEIPT_KIND,
        "source_id": source_id,
        "status": status,
        "checked_at": checked,
        "latest_source_vintage": latest_source_vintage.isoformat()
        if latest_source_vintage
        else None,
        "source_url": source_url,
        "reason": reason,
        "last_success_at": checked if status == "ok" else _previous_success(path),
    }
    directory.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary.replace(path)
    logger.info("Upstream poll receipt: %s", json.dumps(receipt))
    return receipt


def _previous_success(path: Path) -> str | None:
    try:
        previous = json.loads(path.read_text()).get("last_success_at")
    except (OSError, ValueError, AttributeError, RecursionError):
        return None
    return previous if isinstance(previous, str) else None


def _aware(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _vintage(value: object) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _judge(receipt: object, cadence_hours: int, now: datetime) -> dict:
    """One receipt -> {"gap": None | (SourceStatus status, reason), "last_success": ...,
    "vintage": the period the page stated, if readable}."""
    checked = _aware(receipt.get("checked_at")) if isinstance(receipt, dict) else None
    if (
        checked is None
        or receipt.get("receipt_kind") != RECEIPT_KIND
        or receipt.get("status") not in _POLL_STATUSES
    ):
        return {
            "gap": ("missing", "unreadable upstream-poll receipt"),
            "last_success": None,
            "vintage": None,
        }
    success = _aware(receipt.get("last_success_at"))
    view = {
        "last_success": success if success is not None and success <= now else None,
        "vintage": _vintage(receipt.get("latest_source_vintage")),
    }
    age_hours = (now - checked).total_seconds() / 3600
    reason = (
        receipt.get("reason") if isinstance(receipt.get("reason"), str) else "no reason recorded"
    )
    if age_hours < 0:
        return {**view, "gap": ("missing", "upstream-poll receipt dated after the read time")}
    if age_hours > cadence_hours:
        return {
            **view,
            "gap": (
                "stale",
                f"stale poll: last upstream poll older than the {cadence_hours}-hour poll cadence",
            ),
        }
    if receipt["status"] == "failed":
        return {**view, "gap": ("failed", f"last upstream poll failed: {reason}")}
    if receipt["status"] == "unknown":
        return {**view, "gap": ("missing", f"last upstream poll outcome unknown: {reason}")}
    return {**view, "gap": None}


def read_polls(directory: Path, *, now: datetime) -> dict[str, dict]:
    """Monthly id -> its upstream poll's reading. A source with no receipt file is omitted
    (the caller keeps E6's evidence for it); a damaged one reads not-ok and never raises."""
    views = {}
    for monthly_id, source_id in UPSTREAM_POLL_SOURCES.items():
        try:
            raw = (directory / f"{source_id}.json").read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            raw = b""
        views[monthly_id] = _judge(_load(raw), _CADENCE_OF[monthly_id], now)
    return views


def _load(raw: bytes) -> object:
    """A receipt's JSON, or None for anything unreadable: not UTF-8 (UnicodeDecodeError is a
    ValueError), not JSON, or nested too deeply to decode (RecursionError)."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
