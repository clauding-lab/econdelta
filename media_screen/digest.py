"""Format a Discord digest from review candidates for utils.notifier.notify()."""
from __future__ import annotations

from media_screen.types import Candidate


def format_digest(candidates: list[Candidate]) -> tuple[str, str, dict] | None:
    """Return (title, message, fields) for notify(), or None if nothing to report."""
    if not candidates:
        return None
    n = len(candidates)
    title = f"Media screen: {n} candidate{'s' if n != 1 else ''} for review"
    lines = [
        f"**{i+1}. {c.metric_id}** [{c.kind}] — press **{c.press_value}** @ {c.press_as_of} "
        f"vs parsed {c.parsed_value} @ {c.parsed_as_of}\n_{c.source_quote}_ <{c.source_url}>"
        for i, c in enumerate(candidates)
    ]
    message = (
        "\n\n".join(lines)
        + "\n\nReply `approve N` or `reject N` (N = the number above)."
    )
    fields = {c.metric_id: f"{c.press_value} @ {c.press_as_of}" for c in candidates[:10]}
    return title, message, fields
