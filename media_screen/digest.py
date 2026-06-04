"""Format the always-on Discord report (candidates + skips) for utils.notifier.notify().

format_report ALWAYS returns (title, message, fields) — never None — so every run
posts. Candidates are numbered by their REAL media_review.id (None on dry-run).
"""
from __future__ import annotations

from datetime import date

from media_screen.types import Candidate, Skip

# Healthy/expected reasons first, then the eyeball bucket.
_REASON_ORDER = ["already-in-review", "matches-current-data", "older-period",
                 "out-of-range", "no-period"]
_REASON_PHRASE = {
    "already-in-review": "already in review queue",
    "matches-current-data": "matches current data",
    "older-period": "older period than current",
    "out-of-range": "value out of range",
    "no-period": "no explicit period",
}
_MAX_SKIP_LINES = 12


def _period_str(p: date | None) -> str:
    return p.isoformat() if p is not None else "(no period)"


def format_report(
    candidates_with_ids: list[tuple[int | None, Candidate]],
    skips: list[Skip],
    n_tbs: int,
    n_ds: int,
) -> tuple[str, str, dict]:
    n_articles = n_tbs + n_ds
    n_cand = len(candidates_with_ids)

    if n_articles == 0:
        return ("Media screen — 0 articles",
                "Collected 0 articles — all sources failed. No screen this run.", {})

    header = f"Checked {n_articles} articles ({n_tbs} TBS, {n_ds} Daily Star)."

    cand_lines = []
    for rid, c in candidates_with_ids:
        tag = f"#{rid}" if rid is not None else "(dry-run — not queued)"
        parsed = c.parsed_as_of.isoformat() if c.parsed_as_of else "—"
        line = (f"**{tag} {c.metric_id}** [{c.kind}] — press **{c.press_value}** @ "
                f"{c.press_as_of.isoformat()} vs current {c.parsed_value} @ {parsed}\n"
                f"_{c.source_quote}_ <{c.source_url}>")
        if rid is not None:
            line += f"\nReply: approve {rid} · reject {rid}"
        cand_lines.append(line)

    ordered = sorted(
        skips,
        key=lambda s: (_REASON_ORDER.index(s.reason) if s.reason in _REASON_ORDER else 99,
                       s.metric_id),
    )
    skip_lines = [
        f"• {s.metric_id} — {s.value} @ {_period_str(s.period)} → "
        f"{_REASON_PHRASE.get(s.reason, s.reason)}, skipped"
        for s in ordered
    ]
    overflow = ""
    if len(skip_lines) > _MAX_SKIP_LINES:
        overflow = f"\n…and {len(skip_lines) - _MAX_SKIP_LINES} more"
        skip_lines = skip_lines[:_MAX_SKIP_LINES]

    if n_cand > 0:
        title = f"Media screen — {n_cand} needs approval"
        body = [header, ""] + cand_lines
        if skip_lines:
            body += ["", "Also seen (skipped):"] + skip_lines
            body[-1] += overflow
    elif skip_lines:
        title = "Media screen — no change"
        body = [header] + skip_lines
        body[-1] += overflow
        body += ["", "✅ Nothing needs approval."]
    else:
        title = "Media screen — no change"
        body = [header, "No tracked figures in today's articles. No change needed."]

    message = "\n".join(body)
    if len(message) > 3900:  # keep under Discord's 4096 embed-description limit
        message = message[:3900] + "\n…(truncated)"
    fields = {c.metric_id: f"{c.press_value} @ {c.press_as_of.isoformat()}"
              for _, c in candidates_with_ids[:10]}
    return title, message, fields
