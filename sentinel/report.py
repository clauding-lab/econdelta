"""Format the sentinel's one Discord digest, and decide when to speak.

Two shapes:
  * breach digest (warning) — one line per stale metric, worst first;
  * weekly all-fresh heartbeat (info) — so a quiet day is never ambiguous
    between "all fresh" and "the sentinel died" (the run_logs dead-man's-switch
    covers the death; the heartbeat covers the health).
"""
from __future__ import annotations

from .freshness import CHART_FEEDING_METRIC_IDS, FreshnessReport, MetricFreshness

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
        # 2026-08-08 incident (landmine 50): a chart-feeding freeze hid for 5
        # months inside a 41-item digest. Chart-feeding breaches go FIRST
        # under their own heading, worst-first within each group (both lists
        # are filtered from report.breaches, which freshness.assess already
        # sorted worst-first -- filtering preserves that relative order).
        # Total metric lines shown still respects the existing 25-line cap;
        # when there are no chart-feeding breaches this degrades to the
        # exact prior single-list behaviour (no heading added).
        chart_breaches = [m for m in report.breaches if m.metric_id in CHART_FEEDING_METRIC_IDS]
        other_breaches = [m for m in report.breaches if m.metric_id not in CHART_FEEDING_METRIC_IDS]

        lines: list[str] = []
        shown = 0
        if chart_breaches:
            lines.append("CHART-FEEDING — visible to readers:")
            shown_chart = chart_breaches[:_MAX_BREACH_LINES]
            lines.extend(_breach_line(m) for m in shown_chart)
            shown += len(shown_chart)
            if other_breaches:
                lines.append("Other:")
        remaining_budget = _MAX_BREACH_LINES - shown
        shown_other = other_breaches[:remaining_budget] if remaining_budget > 0 else []
        lines.extend(_breach_line(m) for m in shown_other)
        shown += len(shown_other)
        if n_breach > shown:
            lines.append(f"…and {n_breach - shown} more")

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
