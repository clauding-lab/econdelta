"""Format the sentinel's one Discord digest, and decide when to speak.

Two shapes:
  * breach digest (warning) — one line per stale metric, worst first;
  * weekly all-fresh heartbeat (info) — so a quiet day is never ambiguous
    between "all fresh" and "the sentinel died" (the run_logs dead-man's-switch
    covers the death; the heartbeat covers the health).
"""
from __future__ import annotations

from .freshness import FreshnessReport, MetricFreshness

# BD week starts Sunday. Python date.weekday(): Mon=0 … Sun=6.
HEARTBEAT_WEEKDAY = 6

# Keep the digest under Discord's 2000-char embed-description ceiling.
_MAX_BREACH_LINES = 25
_MAX_UNMAPPED_IN_HEARTBEAT = 15


def should_send(report: FreshnessReport, *, is_heartbeat_day: bool) -> bool:
    """Speak on any breach; otherwise only on the weekly heartbeat day.

    Silent non-heartbeat days are fine — the run_logs dead-man's-switch proves
    the sentinel ran even when it says nothing.
    """
    return bool(report.breaches) or is_heartbeat_day


def _breach_line(m: MetricFreshness) -> str:
    return (
        f"`{m.metric_id}` · {m.cadence} · last {m.latest_as_of} · {m.age_days}d old"
    )


def format_digest(report: FreshnessReport) -> tuple[str, str, str, dict]:
    """Return (level, title, message, fields) for ``utils.notifier.notify``."""
    n_breach = len(report.breaches)
    n_fresh = len(report.fresh)
    n_unmapped = len(report.unmapped)

    if n_breach:
        title = f"Freshness sentinel — {n_breach} stale metric(s)"
        lines = [_breach_line(m) for m in report.breaches[:_MAX_BREACH_LINES]]
        if n_breach > _MAX_BREACH_LINES:
            lines.append(f"…and {n_breach - _MAX_BREACH_LINES} more")
        message = "Metrics past their cadence grace window:\n" + "\n".join(lines)
        fields = {
            "Breached": str(n_breach),
            "Fresh": str(n_fresh),
            "Unmapped": str(n_unmapped),
        }
        return "warning", title, message, fields

    # Heartbeat (no breaches).
    title = f"Freshness sentinel — all {n_fresh} fresh"
    message = f"All {n_fresh} mapped metrics are within their cadence grace window."
    if n_unmapped:
        preview = ", ".join(m.metric_id for m in report.unmapped[:_MAX_UNMAPPED_IN_HEARTBEAT])
        more = f" (+{n_unmapped - _MAX_UNMAPPED_IN_HEARTBEAT} more)" if n_unmapped > _MAX_UNMAPPED_IN_HEARTBEAT else ""
        message += (
            f"\n{n_unmapped} metric(s) have no resolvable cadence / no current "
            f"vintage — dedupe/retire candidates: {preview}{more}"
        )
    fields = {"Fresh": str(n_fresh), "Unmapped": str(n_unmapped)}
    return "info", title, message, fields
