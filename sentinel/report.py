"""Format the sentinel's one Discord digest, and decide when to speak.

Two shapes:
  * breach digest (warning) — one line per stale metric, worst first;
  * weekly all-fresh heartbeat (info) — so a quiet day is never ambiguous
    between "all fresh" and "the sentinel died" (the run_logs dead-man's-switch
    covers the death; the heartbeat covers the health).
"""
from __future__ import annotations

from collections import Counter

from .freshness import (
    BRIEF_SURFACED_METRIC_IDS,
    CHART_FEEDING_METRIC_IDS,
    FreshnessReport,
    MetricFreshness,
)

# BD week starts Sunday. Python date.weekday(): Mon=0 … Sun=6.
HEARTBEAT_WEEKDAY = 6

# Keep the digest under Discord's 2000-char embed-description ceiling.
# CHART-FEEDING and BRIEF-SURFACED breaches are shown in full, in COUNT --
# never truncated by a line cap, because being buried past one is exactly the
# failure this exists to prevent (real CPI breaches were once buried at
# digest rank 28-54 inside an undifferentiated "…and 37 more" line). But
# "never capped by count" cannot mean "never capped at all": their combined
# id universe (16 + ~50) could in a worst-case pathological run alone
# approach the character ceiling. The actual enforcement is CHARACTER-
# BUDGET-aware (_fit_other_lines below) applied to the LOWER-PRIORITY "Other"
# tier only -- it shrinks to fit whatever room chart-feeding/brief-surfaced
# left, rather than the old scheme where all three tiers competed for one
# shared LINE-COUNT budget (which is what let "Other" push real breaches
# past line 25 in the first place).
_DISCORD_MESSAGE_CHAR_CAP = 2000
# Headroom reserved for the overflow-summary line itself, future-dated block,
# and parked-line suffix that get appended after the character-budgeted
# "Other" section is built.
_OTHER_SECTION_SAFETY_MARGIN = 250
_MAX_UNMAPPED_IN_HEARTBEAT = 15
_MAX_FUTURE_DATED_LINES = 10

# Coarse prefix -> category label for grouping the "Other" tier's overflow.
# Checked in order; the FIRST match wins, so a more specific prefix must sit
# before a shorter one it would otherwise shadow (none currently overlap,
# but keep future additions ordered narrowest-first).
_CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("dse_close_", "DSE per-ticker closes"),
    ("dse_sector_heat_", "DSE sector heat"),
    ("npl_rate_sub_", "NPL structure (sub-sector)"),
    ("npl_rate_band_", "NPL structure (loan bands)"),
    ("npl_rate_sector_", "NPL structure (sector)"),
    ("npl_rate_cmsme_", "NPL structure (CMSME)"),
    ("npl_", "NPL structure"),
    ("loans_outstanding_band_", "Loan bands"),
    ("lending_share_sector_", "Lending shares"),
    ("deposits_", "Deposit ownership"),
    ("call_money_rate", "Call money tenors"),
    ("crr_utilisation", "Reserve utilisation"),
    ("slr_utilisation", "Reserve utilisation"),
    ("tbill_", "T-bill/T-bond yields"),
    ("tbond_", "T-bill/T-bond yields"),
    ("yield_", "T-bill/T-bond yields"),
)


def _category(metric_id: str) -> str:
    """Coarse grouping label for an "Other"-tier metric id, for the overflow
    summary — never guaranteed exhaustive, only good enough to turn a flat
    "…and 37 more" into something a reader can triage without opening a
    dashboard."""
    for prefix, label in _CATEGORY_PREFIXES:
        if metric_id.startswith(prefix):
            return label
    # Deliberately NOT "Other" -- that word is already the tier heading
    # ("Other (internal/parity, not reader-visible):"), and a naive fallback
    # label would make this overflow line collide with it under substring
    # matching (e.g. a test or reader searching for "Other:").
    return "Uncategorized"


def should_send(report: FreshnessReport, *, is_heartbeat_day: bool) -> bool:
    """Speak on any breach or future-dated anomaly; otherwise only on the
    weekly heartbeat day.

    Silent non-heartbeat days are fine — the run_logs dead-man's-switch proves
    the sentinel ran even when it says nothing.
    """
    return bool(report.breaches) or bool(report.future_dated) or is_heartbeat_day


def _breach_line(m: MetricFreshness) -> str:
    return (
        f"`{m.metric_id}` · {m.cadence} · last {m.latest_as_of} · {m.age_days}d old"
    )


def _future_dated_line(m: MetricFreshness) -> str:
    return f"`{m.metric_id}` · as_of={m.latest_as_of} is {-m.age_days}d in the future"


def _overflow_summary(dropped: list[MetricFreshness]) -> str:
    """"…and N more" grouped by category instead of a flat count."""
    counts = Counter(_category(m.metric_id) for m in dropped)
    by_category = ", ".join(f"{label}: {n}" for label, n in sorted(counts.items()))
    return f"…and {len(dropped)} more ({by_category})"


def _fit_other_lines(
    other_breaches: list[MetricFreshness], *, chars_already_used: int
) -> tuple[list[str], list[MetricFreshness]]:
    """Fit as many "Other"-tier lines as the remaining character budget
    allows, given how much of ``_DISCORD_MESSAGE_CHAR_CAP`` the higher-
    priority sections (chart-feeding, brief-surfaced) already consumed.

    Returns (shown_lines, dropped_metrics). Character-budget-aware rather
    than a fixed line count: a fixed count either wastes budget on a quiet
    day (few chart/brief breaches) or -- the bug this whole restructure
    exists to fix -- lets a fixed shared count push reader-visible metrics
    out of the message entirely on a busy one.
    """
    budget = _DISCORD_MESSAGE_CHAR_CAP - _OTHER_SECTION_SAFETY_MARGIN - chars_already_used
    if budget <= 0:
        return [], list(other_breaches)

    header_cost = len("Other (internal/parity, not reader-visible):\n")
    remaining = budget - header_cost
    shown: list[str] = []
    for i, m in enumerate(other_breaches):
        line = _breach_line(m)
        cost = len(line) + 1  # +1 for the joining newline
        if cost > remaining:
            return shown, other_breaches[i:]
        shown.append(line)
        remaining -= cost
    return shown, []


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
    # "see ACCEPTED_STALE comments" verbosity) to protect Discord's 2000-char
    # embed-description ceiling.
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

    # Future-dated as_of (freshness.py no longer silently discards these) —
    # its own line, prepended to whichever digest shape below actually fires,
    # so a mis-parsed future vintage is never quietly absorbed into "fresh".
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
        # are NEVER truncated: being buried past a line cap is the exact
        # failure this restructure exists to prevent. Only the "Other"
        # (internal/parity-only) tier is capped, and its overflow is grouped
        # by category instead of a flat count.
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

        lines: list[str] = ["Metrics past their cadence grace window:"]
        if chart_breaches:
            lines.append("CHART-FEEDING — visible to readers:")
            lines.extend(_breach_line(m) for m in chart_breaches)
        if brief_breaches:
            lines.append("BRIEF-SURFACED — visible to readers:")
            lines.extend(_breach_line(m) for m in brief_breaches)
        if other_breaches:
            chars_used = len("\n".join(lines)) + 1
            shown_other, dropped = _fit_other_lines(
                other_breaches, chars_already_used=chars_used
            )
            if shown_other or dropped:
                lines.append("Other (internal/parity, not reader-visible):")
                lines.extend(shown_other)
            if dropped:
                lines.append(_overflow_summary(dropped))

        message = "\n".join(lines) + parked_line + future_dated_block
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
    message += parked_line + future_dated_block
    fields = {"Fresh": str(n_fresh), "Unmapped": str(n_unmapped)}
    if n_future:
        fields["Future-dated"] = str(n_future)
    level = "warning" if n_future else "info"
    return level, title, message, fields
