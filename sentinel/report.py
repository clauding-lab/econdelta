"""Format the sentinel's one Discord digest, and decide when to speak.

Two shapes:
  * breach digest (warning) — one line per stale metric, worst first;
  * weekly all-fresh heartbeat (info) — so a quiet day is never ambiguous
    between "all fresh" and "the sentinel died" (the run_logs dead-man's-switch
    covers the death; the heartbeat covers the health).
"""
from __future__ import annotations

from .freshness import (
    BRIEF_SURFACED_METRIC_IDS,
    CHART_FEEDING_METRIC_IDS,
    FreshnessReport,
    MetricFreshness,
)

# BD week starts Sunday. Python date.weekday(): Mon=0 … Sun=6.
HEARTBEAT_WEEKDAY = 6

# Discord's embed-description ceiling is 2000 chars; a HARD budget of 1900
# leaves headroom for the title/fields (a separate field on the notify()
# call, but Discord's embed as a WHOLE has its own overall size ceiling too)
# and for the tail line this module always appends when anything was
# trimmed. This is enforced over the FULL assembled message (breach section
# + parked line + future-dated block), not just the breach lines in
# isolation -- 2026-08-22 round-1 review HIGH-3: a prior version budgeted
# only the "Other" tier and could still overflow once a parked line or a
# large future-dated block was appended on top, which is exactly the broad-
# outage case (many chart-feeding/brief-surfaced breaches AND several
# future-dated rows at once) where notifier.py has no truncation of its own
# and Discord's webhook API 400s a too-long embed -- silently dropping the
# ENTIRE digest, not just the tail of it.
_HARD_MESSAGE_CHAR_BUDGET = 1900
_MAX_UNMAPPED_IN_HEARTBEAT = 15
_MAX_FUTURE_DATED_LINES = 10


def should_send(report: FreshnessReport, *, is_heartbeat_day: bool) -> bool:
    """Speak on any breach or future-dated anomaly; otherwise only on the
    weekly heartbeat day.

    Silent non-heartbeat days are fine — the run_logs dead-man's-switch proves
    the sentinel ran even when it says nothing. ``report.future_dated`` has
    already had ``ACCEPTED_FUTURE_DATED_METRIC_IDS`` filtered out by
    ``freshness.assess`` before this ever runs, so a known, already-diagnosed
    future-dated mis-parse (debt_gdp_ratio) can never by itself flip this to
    True on a quiet day.
    """
    return bool(report.breaches) or bool(report.future_dated) or is_heartbeat_day


def _breach_line(m: MetricFreshness) -> str:
    return (
        f"`{m.metric_id}` · {m.cadence} · last {m.latest_as_of} · {m.age_days}d old"
    )


def _future_dated_line(m: MetricFreshness) -> str:
    return f"`{m.metric_id}` · as_of={m.latest_as_of} is {-m.age_days}d in the future"


def _render_breach_section(
    chart_lines: list[str], brief_lines: list[str], other_lines: list[str]
) -> str:
    lines: list[str] = []
    if chart_lines:
        lines.append("CHART-FEEDING — visible to readers:")
        lines.extend(chart_lines)
    if brief_lines:
        lines.append("BRIEF-SURFACED — visible to readers:")
        lines.extend(brief_lines)
    if other_lines:
        lines.append("Other (internal/parity, not reader-visible):")
        lines.extend(other_lines)
    return "\n".join(lines)


def _fit_breach_section(
    chart_breaches: list[MetricFreshness],
    brief_breaches: list[MetricFreshness],
    other_breaches: list[MetricFreshness],
    *,
    budget: int,
) -> str:
    """Render the three-tier breach section, trimming lines one at a time
    from the LOWEST-priority non-empty tier (Other, then brief-surfaced,
    then chart-feeding as an absolute last resort) until it fits ``budget``
    characters, and always appending a hidden-count tail line when anything
    was cut.

    Chart-feeding and brief-surfaced are never trimmed FIRST -- they are the
    whole point of this restructure (2026-08-08 landmine 50: a chart-feeding
    freeze once hid inside an undifferentiated overflow line). But "trimmed
    last" is not "never trimmed": in a genuinely pathological run (dozens of
    reader-visible breaches at once, real production ceiling ~66 possible
    ids), even the reader-visible tiers alone could exceed Discord's cap, and
    silently exceeding it means the notifier 400s and the ENTIRE digest is
    dropped -- worse than a partial one. Worst-first ordering is preserved
    throughout (report.breaches already sorted worst-first; trimming drops
    from the END of each list, so the WORST breaches in every tier are the
    ones kept the longest).
    """
    shown_chart = [_breach_line(m) for m in chart_breaches]
    shown_brief = [_breach_line(m) for m in brief_breaches]
    shown_other = [_breach_line(m) for m in other_breaches]
    hidden = {"chart-feeding": 0, "brief-surfaced": 0, "other": 0}

    def render() -> str:
        section = _render_breach_section(shown_chart, shown_brief, shown_other)
        total_hidden = sum(hidden.values())
        if total_hidden:
            parts = [f"{n} {label}" for label, n in hidden.items() if n]
            section += f"\n+{total_hidden} more ({', '.join(parts)}) hidden"
        return section

    # Trim ONE line at a time from the lowest-priority non-empty tier,
    # re-checking the FULLY rendered output (tail line included, since it
    # grows as more gets hidden) against budget every iteration -- this is
    # what guarantees the hard cap holds even accounting for the tail line's
    # own length, rather than reserving a fixed, possibly-wrong-sized margin
    # for it up front.
    while len(render()) > budget and (shown_other or shown_brief or shown_chart):
        if shown_other:
            shown_other.pop()
            hidden["other"] += 1
        elif shown_brief:
            shown_brief.pop()
            hidden["brief-surfaced"] += 1
        else:
            shown_chart.pop()
            hidden["chart-feeding"] += 1

    return render()


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
    n_future = len(report.future_dated)

    # Built once, appended to whichever branch below actually returns --
    # empty string (a no-op append) on any non-heartbeat day or when there's
    # nothing parked to report. Kept to ONE compact line (no "frozen at" /
    # "see ACCEPTED_STALE comments" verbosity) to protect the hard message
    # character budget below.
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

    # Future-dated as_of (freshness.py no longer silently discards these,
    # minus ACCEPTED_FUTURE_DATED_METRIC_IDS which assess() already
    # excluded) — its own line, prepended to whichever digest shape below
    # actually fires, so a mis-parsed future vintage is never quietly
    # absorbed into "fresh".
    future_dated_block = ""
    if report.future_dated:
        shown_future = report.future_dated[:_MAX_FUTURE_DATED_LINES]
        future_lines = [_future_dated_line(m) for m in shown_future]
        if n_future > len(shown_future):
            future_lines.append(f"…and {n_future - len(shown_future)} more")
        future_dated_block = (
            f"\n\nFUTURE-DATED as_of ({n_future}) — likely a mis-parse, never "
            "read as this week's vintage:\n" + "\n".join(future_lines)
        )

    suffix = parked_line + future_dated_block

    if n_breach:
        title = f"Freshness sentinel — {n_breach} stale metric(s)"
        if n_future:
            title += f", {n_future} future-dated"
        # 2026-08-08 incident (landmine 50): a chart-feeding freeze hid for 5
        # months inside a 41-item digest. Reader-visible breaches — CHART-
        # FEEDING (metric_history_monthly charts) and BRIEF-SURFACED (daily
        # metric_history ids The Brief's sections render) — go FIRST under
        # their own headings, worst-first within each group (all three lists
        # are filtered from report.breaches, which freshness.assess already
        # sorted worst-first -- filtering preserves that relative order), and
        # are trimmed LAST, not never: see _fit_breach_section's docstring
        # for why "never truncated by a line cap" still needs a hard
        # character-budget backstop (HIGH-3, 2026-08-22 round-1 review).
        chart_breaches = [m for m in report.breaches if m.metric_id in CHART_FEEDING_METRIC_IDS]
        brief_breaches = [
            m for m in report.breaches
            if m.metric_id in BRIEF_SURFACED_METRIC_IDS
            and m.metric_id not in CHART_FEEDING_METRIC_IDS
        ]
        other_breaches = [
            m for m in report.breaches
            if m.metric_id not in CHART_FEEDING_METRIC_IDS
            and m.metric_id not in BRIEF_SURFACED_METRIC_IDS
        ]

        header = "Metrics past their cadence grace window:"
        budget = _HARD_MESSAGE_CHAR_BUDGET - len(header) - 1 - len(suffix)
        section = _fit_breach_section(chart_breaches, brief_breaches, other_breaches, budget=budget)
        message = header + "\n" + section + suffix
        fields = {
            "Breached": str(n_breach),
            "Fresh": str(n_fresh),
            "Unmapped": str(n_unmapped),
        }
        if n_future:
            fields["Future-dated"] = str(n_future)
        return "warning", title, message, fields

    # Heartbeat (no breaches, but possibly future-dated anomalies).
    title = f"Freshness sentinel — all {n_fresh} fresh"
    if n_future:
        title += f", {n_future} future-dated"
    message = f"All {n_fresh} mapped metrics are within their cadence grace window."
    if n_unmapped:
        preview = ", ".join(m.metric_id for m in report.unmapped[:_MAX_UNMAPPED_IN_HEARTBEAT])
        more = f" (+{n_unmapped - _MAX_UNMAPPED_IN_HEARTBEAT} more)" if n_unmapped > _MAX_UNMAPPED_IN_HEARTBEAT else ""
        message += (
            f"\n{n_unmapped} metric(s) have no resolvable cadence / no current "
            f"vintage — dedupe/retire candidates: {preview}{more}"
        )
    message += suffix
    fields = {"Fresh": str(n_fresh), "Unmapped": str(n_unmapped)}
    if n_future:
        fields["Future-dated"] = str(n_future)
    level = "warning" if n_future else "info"
    return level, title, message, fields
