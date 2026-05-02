"""Opus 4.6 high-effort review of aggregated latest.json before commit.

Compares today's proposed `data` dict against the last N days of latest.json
files (archived at /home/.../econdelta/data/archive/latest_YYYY-MM-DD.json),
looking for missing indicators, anomalous values, and structural drift.

Returns a structured verdict. On any operational error (binary missing,
timeout, malformed output) the verdict defaults to status="ok" with a
review_skipped reason — a broken review tool must never block publication.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("opus_review")

REVIEW_PROMPT = """You are reviewing today's Bangladesh economic indicators data file (latest.json) before it ships to a daily brief read by senior bankers at IDLC Finance PLC.

Your job: identify any issues that would make today's data unsafe to publish.

Look for, in order of severity:
1. MISSING DATA — indicators with non-null values in the historical days but null/zero today (excluding Friday-Saturday weekend closures for trading-day indicators like dsex/advancing/declining).
2. ANOMALIES — values that move more than 5 percent day-over-day for stable indicators (FX rates, inflation, reserves, policy rate), or more than 50 percent for volatile ones (trading volumes, commodity intra-day moves).
3. STRUCTURAL DRIFT — large unexpected indicator additions or removals.
4. INTERNAL INCONSISTENCY — e.g., usd_bdt_mid disagreeing materially with the average of usd_bdt_buy and usd_bdt_sell; broad_money lower than reserve_money.

Bangladesh-context calibration:
- USD/BDT typically moves under 0.5 percent per day under managed float; more than 2 percent in a day is suspicious.
- DSE indices closed Friday and Saturday (weekend) — null/zero values on those days are correct.
- Bangladesh Bank monthly indicators (BoP, GDP, inflation) only update once a month; same value across many days is normal for these.
- Reserves typically change by under 1 percent week-over-week.

Return ONLY a single JSON object, no commentary, no code fences. Schema:
{{
  "status": "ok" | "reject",
  "reason": "1-line summary",
  "missing": ["indicator_id", ...],
  "anomalies": [
    {{"indicator": "id", "today": value, "recent_median": value, "comment": "1-line"}}
  ],
  "confidence": 0.0
}}

Use status="reject" only when there are real publication-blocking concerns.
Mild data drift, weekend closures, and same-value-as-yesterday for monthly
indicators are all OK and should produce status="ok".

Today's date (UTC): {today_str}

TODAY'S PROPOSED DATA (the `.data` block of latest.json):
{today_json}

LAST {n_days} DAYS OF HISTORICAL DATA (oldest first):
{history_json}
"""


def _truncate(s: str, max_chars: int) -> str:
    """Return s if shorter than max_chars; otherwise truncate with a marker."""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"\n... [truncated, original {len(s)} chars]"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first {...} JSON object out of a possibly noisy LLM response."""
    text = text.strip()
    if text.startswith("```"):
        # Strip ``` json ... ``` fences
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to greedy match of outermost {...}
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def review_data(
    today_data: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    binary: str | None = None,
    model: str = "claude-opus-4-7",
    timeout_s: int = 300,
) -> dict[str, Any]:
    """Submit today's data + history to Opus for review.

    Returns a verdict dict. On any operational error returns
    {"status": "ok", "reason": "review_skipped: <err>"} so the review tool
    being broken never blocks the brief from publishing.
    """
    binary = binary or os.environ.get("CLAUDE_BINARY", "claude")

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_json = _truncate(json.dumps(today_data, indent=2, default=str), 50_000)
    history_json = _truncate(json.dumps(history, indent=2, default=str), 200_000)

    prompt = REVIEW_PROMPT.format(
        today_str=today_str,
        today_json=today_json,
        n_days=len(history),
        history_json=history_json,
    )

    try:
        result = subprocess.run(
            [binary, "--print", "--model", model],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("opus review unavailable: %s", e)
        return {"status": "ok", "reason": f"review_skipped: {type(e).__name__}", "skipped": True}

    if result.returncode != 0:
        logger.warning("opus review exit %d: %s", result.returncode, result.stderr.strip()[:200])
        return {
            "status": "ok",
            "reason": f"review_skipped: claude_exit_{result.returncode}",
            "skipped": True,
        }

    verdict = _extract_json_object(result.stdout)
    if verdict is None:
        logger.warning("opus review output not parseable: %s", result.stdout[:300])
        return {"status": "ok", "reason": "review_skipped: malformed_output", "skipped": True}

    # Normalise required keys
    verdict.setdefault("status", "ok")
    verdict.setdefault("reason", "")
    verdict.setdefault("missing", [])
    verdict.setdefault("anomalies", [])
    if verdict["status"] not in ("ok", "reject"):
        logger.warning("opus review returned unexpected status %r — treating as ok", verdict["status"])
        verdict["status"] = "ok"
    return verdict


def archive_latest(latest_path: Path, archive_dir: Path) -> Path | None:
    """Copy latest.json to archive/latest_<UTC-date>.json (overwrites same-day).

    Returns the archive path on success, None on failure (logged, not raised).
    """
    if not latest_path.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dst = archive_dir / f"latest_{today_utc}.json"
    try:
        dst.write_bytes(latest_path.read_bytes())
        return dst
    except OSError as e:
        logger.warning("archive write failed: %s", e)
        return None


def load_history(archive_dir: Path, days: int = 5) -> list[dict[str, Any]]:
    """Load up to `days` most recent archived latest.json files.

    Each loaded file is reduced to its `.data` dict — Opus only needs to
    review the flat indicator values, not freshness/alerts/domains metadata.
    """
    if not archive_dir.exists():
        return []
    candidates = sorted(archive_dir.glob("latest_*.json"))[-days:]
    out: list[dict[str, Any]] = []
    for p in candidates:
        try:
            blob = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        # Keep just the data block + updated_at for context
        out.append({
            "updated_at": blob.get("updated_at"),
            "data": blob.get("data", {}),
        })
    return out
