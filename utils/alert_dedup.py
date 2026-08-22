"""Cross-process, cross-day alert dedup for notify() call sites that can't
rely on utils.notifier's own in-memory (level, title) dedup.

That in-memory dedup only survives within ONE process's lifetime -- but
systemd's ``Restart=on-failure`` can retry a crashed unit up to 3-4 times
inside its ``StartLimitBurst`` window (AGENTS.md landmine 48), and EACH
restart is a fresh process with an empty dedup cache. Without a state file
that survives across those restarts, the exact same failure alerts up to 4
times in quick succession.

Mirrors the ``last_alerted`` day-stamp pattern already used by
``utils.staleness.check_watchlist_staleness`` (MEDIUM-5, 2026-08-22 round-1
review): one alert per (key, day), tracked in a small JSON state file.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


def should_alert_today(key: str, state_path: Path, *, today: date) -> bool:
    """True if ``key`` has not already alerted today -- and records that it
    has, so a caller that gets True back and then calls notify() will get
    False on any retry within the same day.

    Best-effort by design: any read/write failure defaults to "alert"
    (never silently suppress a REAL alert because of a state-file problem)
    and never raises -- a broken dedup file must not become a second reason
    a failure goes unreported.
    """
    state: dict[str, str] = {}
    try:
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if not isinstance(state, dict):
                state = {}
    except (OSError, ValueError) as e:
        logger.warning("alert dedup state unreadable (%s) — alerting anyway", e)
        state = {}

    if state.get(key) == today.isoformat():
        return False

    state[key] = today.isoformat()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
        os.replace(tmp, state_path)
    except OSError as e:
        logger.warning("alert dedup state write failed (%s) — alerting anyway", e)
    return True
