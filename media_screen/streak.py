"""Tracks consecutive zero-insert media-screen runs and alerts once the
streak crosses a threshold.

The screen legitimately inserts 0 candidates on most runs -- most collected
articles never state a machine-parseable period for a tracked BB indicator,
so "no candidates" is not itself a bug (see media_screen/filter.py's classify()
docstring, and AGENTS.md landmine 27). But a screen that NEVER inserts
anything, run after run, for weeks, is a different signal entirely --
something in the collect -> extract -> catalog-match -> classify -> insert
chain has silently broken. This module is the trip-wire for that.

Named "runs", not "nights": the nightly systemd timer is the normal caller,
but a manual `python -m scrapers.media_screen --url ...` debugging run also
calls update_zero_insert_streak and touches the same counter -- the streak is
consecutive CALLS, not consecutive calendar nights.

62 consecutive zero-insert NIGHTS (2026-06-04 -> 2026-08-04, all of them the
scheduled 21:30 BDT systemd run -- no manual runs in that window) ran
completely undetected before this existed -- econdelta-media-screen.service
exited 0 every single night, so nothing else would have caught it. See
AGENT_LEARNINGS.md (2026-08-05) and AGENTS.md landmine 47.

Mirrors utils/staleness.py's state-file conventions (systemd starts a fresh
process each run, so the counter must be a file, not a module global; treat
a damaged file as "start over", never as a reason to crash the run; write via
temp-file + atomic replace so a killed-mid-write process can't corrupt it) --
with one deliberate difference: a WRITE failure here also fires a Discord
warning (unlike staleness.py's log-only), because this file's entire job is
being the tripwire when everything else stays quiet -- a tripwire whose own
failure is silent defeats its own purpose. `logs/media_screen-systemd.log`
(the unit's `StandardOutput=`/`StandardError=` target, deploy/econdelta-
media-screen.service) is where the per-article disposition lines referenced
in the alert body actually live -- NOT `logs/econdelta-media-screen-
systemd.log`, a filename that doesn't exist on disk.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Callable

from utils.notifier import notify

logger = logging.getLogger("media_screen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STREAK_PATH = _REPO_ROOT / "data" / "media_screen_zero_insert_streak.json"

# Consecutive zero-insert runs before the silence itself becomes the alert.
# One week of nightly runs: long enough that a genuinely quiet news week
# can't false-positive (most runs are legitimately zero), short enough that
# a real regression surfaces inside a week rather than the 62 days this one
# took.
ALERT_THRESHOLD = 7


def _load_streak(path: Path) -> int:
    """Read the persisted streak, treating any damage as "start over" -- the
    same non-authoritative-cache posture as utils/staleness._load_state."""
    if not path.exists():
        return 0
    try:
        blob = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("media screen: zero-insert streak state unreadable (%s) — restarting", e)
        return 0
    streak = blob.get("consecutive_zero_insert_runs")
    return streak if isinstance(streak, int) and streak >= 0 else 0


def _write_streak(
    path: Path, streak: int, today: date, *, notifier: Callable[..., object],
) -> None:
    """Persist via temp file + atomic replace (systemd can kill mid-write).

    Unlike utils/staleness.py's write helper, a failure here ALSO notifies
    (not just logs): this counter is itself an alarm, and an alarm that can
    fail silently is worse than no alarm -- the box has already shown a
    root-owned-file class of write failure once (PR #115).
    """
    payload = {"generated_at": today.isoformat(), "consecutive_zero_insert_runs": streak}
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("media screen: zero-insert streak state write failed: %s", e)
        notifier(
            "warning",
            "media screen — zero-insert streak tracker write failed",
            f"Could not persist the zero-insert streak counter to {path}: {e}. "
            "The N-consecutive-zero-insert alert cannot fire reliably until "
            "this is fixed -- treat this as its own incident, not routine noise.",
        )


def update_zero_insert_streak(
    n_inserted: int,
    *,
    today: date,
    state_path: Path = DEFAULT_STREAK_PATH,
    notifier: Callable[..., object] = notify,
) -> int:
    """Update the consecutive-zero-insert counter for this run and alert once
    it crosses ``ALERT_THRESHOLD``. Returns the streak AFTER this run.

    A real insert (``n_inserted > 0``) resets the streak to 0 -- the screen is
    proven to be working end-to-end again. A zero-insert run increments it;
    once the streak reaches a multiple of ``ALERT_THRESHOLD``, one Discord
    warning fires -- and re-fires every ``ALERT_THRESHOLD`` runs after that
    (not just once), so a screen that stays broken doesn't go quiet again for
    another two months.
    """
    streak = 0 if n_inserted > 0 else _load_streak(state_path) + 1
    _write_streak(state_path, streak, today, notifier=notifier)

    if streak > 0 and streak % ALERT_THRESHOLD == 0:
        notifier(
            "warning",
            f"media screen — {streak} consecutive zero-insert runs",
            f"The media screen has inserted 0 candidates into media_review for "
            f"{streak} consecutive run(s). Zero is a normal outcome most runs "
            f"(most articles don't state a machine-parseable period for a "
            f"tracked figure), but a streak this long usually means something "
            f"between screening and insert is silently broken -- extraction, "
            f"catalog matching, or a stale-date comparison -- rather than a "
            f"genuine absence of press-worthy figures. Check "
            f"logs/media_screen-systemd.log for the per-article disposition "
            f"lines (each collected article now logs why it was skipped, "
            f"screened out, or inserted).",
        )
        logger.warning("media screen: %d consecutive zero-insert run(s) — alert fired", streak)
    return streak
