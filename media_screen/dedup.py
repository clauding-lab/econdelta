"""Drop candidates that already have an open (pending) or just-rejected row, so
the same article doesn't re-ping day after day."""
from __future__ import annotations

from media_screen.types import Candidate


def drop_already_open(candidates: list[Candidate], open_rows: list[dict]) -> list[Candidate]:
    seen = {(r["metric_id"], str(r["press_as_of"])[:10]) for r in open_rows}
    return [c for c in candidates if (c.metric_id, c.press_as_of.isoformat()) not in seen]
