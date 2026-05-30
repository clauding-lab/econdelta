"""Weekly ALCO briefing orchestrator. Run: `python -m briefing`.

Flow: collect history -> freshness gate -> compute anomalies -> call Claude
(writer) -> validate -> assemble row -> upsert. Returns an exit code that
wrap_run maps to run_logs.status (0 ok / 1 fail / 2 stale).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

from briefing import config
from briefing.anomalies import compute_candidates
from briefing.freshness import assess_freshness
from briefing.prompt import BriefingValidationError, build_prompt, validate_output
from claude_max.max_client import MaxCallError, run_max
from utils.notifier import notify
from utils.supabase_reader import get_metric_history, get_recent_briefings, get_recent_run_ok
from utils.supabase_writer import upsert_briefing

logger = logging.getLogger("briefing")

MODEL = os.environ.get("BRIEFING_MODEL", "opus[1m]")
EFFORT = os.environ.get("BRIEFING_EFFORT", "xhigh")
HISTORY_DAYS = 120
PRIOR_BRIEFINGS = 8
AGGREGATE_FRESH_HOURS = 48
RUN_MAX_TIMEOUT_S = 1800


# Thin indirections so tests can patch config loading cheaply.
def _indicators() -> list[dict]:
    return config.load_indicators()


def _thresholds(indicators):
    return config.thresholds_by_metric(indicators)


def _cadence(indicators):
    return config.cadence_by_metric(indicators)


def _labels(indicators):
    return config.label_by_metric(indicators)


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _collect_history(metric_ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for mid in metric_ids:
        rows = get_metric_history(mid, days=HISTORY_DAYS)
        if rows:
            out[mid] = rows
    return out


def _latest_as_of(history: dict[str, list[dict]]) -> dict[str, date]:
    out: dict[str, date] = {}
    for mid, rows in history.items():
        if rows:
            out[mid] = date.fromisoformat(rows[0]["as_of"])
    return out


def _build_digest(history: dict[str, list[dict]], labels: dict[str, str]) -> dict:
    digest = {}
    for mid, rows in history.items():
        vals = [float(r["value"]) for r in rows if r.get("value") is not None]
        if not vals:
            continue
        digest[mid] = {
            "label": labels.get(mid, mid),
            "latest": vals[0],
            "as_of": rows[0]["as_of"],
            "n": len(vals),
            "mean_recent": round(sum(vals[:30]) / len(vals[:30]), 4),
        }
    return digest


def main() -> int:
    today = datetime.now(timezone.utc).date()
    week_of = _monday_of(today)

    indicators = _indicators()
    tracked = config.tracked_metric_ids(indicators)
    history = _collect_history(tracked)
    if not history:
        notify("error", "Weekly briefing: no metric history", "metric_history read returned nothing.")
        return 1

    aggregate_ok = get_recent_run_ok("aggregate", within_hours=AGGREGATE_FRESH_HOURS)
    fresh = assess_freshness(_latest_as_of(history), _cadence(indicators),
                             set(config.CORE_METRIC_IDS), today, aggregate_ok)
    if fresh.core_stale:
        notify("warning", "Weekly briefing skipped — core data stale",
               "Did not generate this week; keeping last good briefing.\n" + "\n".join(fresh.reasons))
        return 2  # -> run_logs status 'stale'

    candidates = compute_candidates(history, _thresholds(indicators),
                                    _cadence(indicators), _labels(indicators))
    candidate_by_id = {c.candidate_id: c for c in candidates}
    candidate_payload = [
        {"candidate_id": c.candidate_id, "metric_id": c.metric_id, "label": c.label,
         "stat": c.stat, "value": c.value, "detail": c.detail, "severity": c.severity}
        for c in candidates
    ]

    prior = get_recent_briefings(limit=PRIOR_BRIEFINGS)
    open_threads = prior[0]["open_threads"] if prior and prior[0].get("open_threads") else []
    digest = _build_digest(history, _labels(indicators))
    prompt = build_prompt(digest=digest, candidates=candidate_payload,
                          prior_briefings=prior, open_threads=open_threads, week_of=week_of.isoformat())

    try:
        result = run_max(prompt=prompt, model=MODEL, effort=EFFORT, timeout_s=RUN_MAX_TIMEOUT_S)
    except MaxCallError as e:
        notify("error", "Weekly briefing: Claude call failed", str(e))
        return 1

    try:
        out = validate_output(result.parsed, set(candidate_by_id))
    except BriefingValidationError as e:
        notify("error", "Weekly briefing: invalid model output", f"{e}\nraw: {result.raw_text[:500]}")
        return 1

    # Merge Python's authoritative numbers with Claude's curation.
    featured = []
    for f in out["featured_anomalies"]:
        c = candidate_by_id[f["candidate_id"]]
        featured.append({
            "candidate_id": c.candidate_id, "label": c.label, "stat": c.stat,
            "value": c.value, "detail": c.detail, "severity": c.severity,
            "metric_id": c.metric_id, "why": f["why"],
        })

    row = {
        "week_of": week_of.isoformat(),
        "title": out["title"],
        "body": out["body"],
        "featured_anomalies": featured,
        "open_threads": out.get("updated_threads", []),
        "data_as_of": fresh.data_as_of.isoformat(),
        "stale_series": fresh.stale_series,
        "model": MODEL,
        "effort": EFFORT,
        "total_cost_usd": result.total_cost_usd,
        "inputs_hash": hashlib.sha256(json.dumps(digest, sort_keys=True, default=str).encode()).hexdigest()[:16],
    }
    upsert_briefing(row)
    logger.info("briefing written for week_of=%s (cost=%s)", week_of, result.total_cost_usd)
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("briefing", "econdelta-briefing.service", main))
