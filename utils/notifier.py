"""Discord webhook alerts with rate-limit and dry-run support."""

import logging
import os
import time
from typing import Literal

import requests

logger = logging.getLogger(__name__)

# In-memory dedup: maps (level, title) -> last-sent epoch seconds
_recent_alerts: dict[tuple[str, str], float] = {}
_DEDUP_WINDOW_SECONDS = 3600

_LEVEL_EMOJI: dict[str, str] = {
    "info": "\u2139\ufe0f",      # information symbol
    "warning": "\u26a0\ufe0f",   # warning sign
    "error": "\U0001f6a8",       # rotating light
}

_LEVEL_COLOR: dict[str, int] = {
    "info": 0x3498DB,
    "warning": 0xF39C12,
    "error": 0xE74C3C,
}


def notify(
    level: Literal["info", "warning", "error"],
    title: str,
    message: str,
    fields: dict | None = None,
) -> bool:
    """Send a Discord embed alert.

    Args:
        level: Severity — "info", "warning", or "error".
        title: Short alert title shown in the embed heading.
        message: Body text of the alert.
        fields: Optional dict of {name: value} pairs added as embed fields
                (displayed inline by default).

    Returns:
        True if the alert was sent (or printed in dry-run mode).
        False if the webhook URL is not configured or the request failed.
        False (with suppressed notice) if the same (level, title) was sent
        within the last 3600 seconds.
    """
    dedup_key = (level, title)
    now = time.monotonic()
    last_sent = _recent_alerts.get(dedup_key)
    if last_sent is not None and (now - last_sent) < _DEDUP_WINDOW_SECONDS:
        print(
            f"[NOTIFIER] Suppressed duplicate alert ({level!r}, {title!r}): "
            f"already sent {now - last_sent:.0f}s ago"
        )
        return False

    dry_run = os.environ.get("ECONDELTA_DRY_RUN", "0") == "1"
    if dry_run:
        print(f"[DRY-RUN DISCORD] {level} {title}: {message}")
        _recent_alerts[dedup_key] = now
        return True

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logger.warning(
            "DISCORD_WEBHOOK_URL is not set — skipping alert (%s: %s)", level, title
        )
        return False

    emoji = _LEVEL_EMOJI.get(level, "")
    embed: dict = {
        "title": f"{emoji} EconDelta \u2014 {title}",
        "description": message,
        "color": _LEVEL_COLOR.get(level, 0x95A5A6),
    }

    if fields:
        embed["fields"] = [
            {"name": str(k), "value": str(v), "inline": True} for k, v in fields.items()
        ]

    payload = {"embeds": [embed]}

    try:
        response = requests.post(webhook_url, json=payload, timeout=5)
        response.raise_for_status()
        _recent_alerts[dedup_key] = now
        return True
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to send Discord alert (%s: %s): %s", level, title, exc)
        return False


if __name__ == "__main__":
    # Self-test: set ECONDELTA_DRY_RUN=1 to avoid real network calls
    os.environ.setdefault("ECONDELTA_DRY_RUN", "1")

    notify("info", "Self-test info", "This is an info-level test alert.")
    notify("warning", "Self-test warning", "This is a warning-level test alert.")
    notify("error", "Self-test error", "This is an error-level test alert.", fields={"metric": "dsex", "value": "5500"})
