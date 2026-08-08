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


def format_digest(
    report: FreshnessReport, *, is_heartbeat_day: bool = False
) -> tuple[str, str, str, dict]:
    """Return (level, title, message, fields) for ``utils.notifier.notify``.

    ``is_heartbeat_day`` (2026-08-08 review R3, re-review of M5): gates the
    "Chart-feeding, parked: ..." line onto BOTH digest shapes below, not
    just the no-breach one. The original M5 fix only ever attached that
    line inside the ``n_breach == 0`` branch -- the re-reviewer proved that
    is "effectively never" seen in practice, because most actual heartbeat
    days (Sundays) DO have at least one breach, which routes through the
    OTHER branch entirely and never reaches the parked-line code at all.
    Gating on the real calendar heartbeat day (passed in by the caller,
    ``sentinel/main.py``'s own ``today.weekday() == HEARTBEAT_WEEKDAY``)
    instead of on breach-count gives weekly visibility regardless of
    whether today also happens to have breaches.
    """
    n_breach = len(report.breaches)
    n_fresh = len(report.fresh)
    n_unmapped = len(report.unmapped)

    # Built once, appended to whichever branch below actually returns --
    # empty string (a no-op append) on any non-heartbeat day or when there's
    # nothing parked to report. Kept to ONE compact line (no "frozen at" /
    # "see ACCEPTED_STALE comments" verbosity) to protect Discord's 2000-char
    # embed-description ceiling, which a full 25-line breach digest already
    # sits close to (2026-08-08 review R3 budget note).
    parked_chart_feeding = (
        [s for s in report.accepted_stale if s.metric_id in CHART_FEEDING_METRIC_IDS]
        if is_heartbeat_day
        else []
    )
    parked_line = ""
    if parked_chart_feeding:
        parked_desc = ", ".join(
            f"{s.metric_id} ({s.latest_as_of.strftime('%Y-%m') if s.latest_as_of else '?'})"
            for s in parked_chart_feeding
        )
        parked_line = f"\nChart-feeding, parked: {parked_desc}"

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
        # 2026-08-08 review L1: compute the "Other:" section BEFORE deciding
        # whether to print its heading -- a chart-feeding group that already
        # fills the whole 25-line budget must NOT print a dangling "Other:"
        # heading with zero lines under it (the prior version added the
        # heading unconditionally whenever other_breaches was non-empty,
        # regardless of remaining budget).
        remaining_budget = _MAX_BREACH_LINES - shown
        shown_other = other_breaches[:remaining_budget] if remaining_budget > 0 else []
        if shown_other:
            if chart_breaches:
                lines.append("Other:")
            lines.extend(_breach_line(m) for m in shown_other)
            shown += len(shown_other)
        if n_breach > shown:
            lines.append(f"…and {n_breach - shown} more")

        message = "Metrics past their cadence grace window:\n" + "\n".join(lines) + parked_line
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
    message += parked_line
    fields = {"Fresh": str(n_fresh), "Unmapped": str(n_unmapped)}
    return "info", title, message, fields
