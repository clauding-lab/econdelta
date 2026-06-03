"""Apply an approve/reject decision to a media_review row, + a manual CLI.

Copotron (Hetzner) normally performs the equivalent Supabase PATCH directly
(see docs/media-screen-copotron-wiring.md). This module is the canonical,
tested decision path and a CLI for the ExonVPS box:
    python -m media_screen.decide approve 7 --actor discord:adnan
Approved rows are applied by EconDelta's aggregate (Phase 2); rejected rows are
discarded (no metric_history change).
"""
from __future__ import annotations

import argparse
import sys

from utils.supabase_writer import decide_media_review


def apply_decision(review_id, decision, *, actor, decider=decide_media_review) -> dict:
    """Flip the row and return a friendly result. ok=False if it wasn't pending."""
    updated = decider(review_id, decision, actor=actor)
    if updated:
        return {
            "ok": True,
            "review_id": int(review_id),
            "message": f"media_review {review_id} → {decision}d by {actor}",
        }
    return {
        "ok": False,
        "review_id": int(review_id),
        "message": (
            f"media_review {review_id} not pending (already decided or not found) — no change"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("decision", choices=["approve", "reject"])
    ap.add_argument("review_id", type=int)
    ap.add_argument("--actor", default="cli")
    a = ap.parse_args()
    result = apply_decision(a.review_id, a.decision, actor=a.actor)
    print(result["message"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
