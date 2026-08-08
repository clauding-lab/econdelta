"""Capture the log tail behind a run, and scrub it before it becomes ``error=``.

Why this exists: ``run_logs.error`` only used to populate when a scraper
raised an UNCAUGHT exception. A scraper that catches its own failure and
returns exit 1 (``fail``) or 2 (``stale``) left ``error`` null — undiagnosable
from the table. ``utils.supabase_writer.wrap_run`` attaches a
``RingBufferHandler`` to the root logger for the duration of the run so a
WARNING-or-above log line written just before the scraper gives up becomes
the diagnostic, without every module having to thread a message back to
``wrap_run`` explicitly.

CRITICAL: ``run_logs`` is a PUBLIC table (read by the PWA Runs page with the
anon key, landmine 18). Anything placed in ``error`` is world-readable.
``scrub_secrets`` is the single choke point before that happens — it is
deliberately aggressive: a false-positive redaction is harmless, an
unredacted secret on a public table is not.
"""
from __future__ import annotations

import logging
import re
from collections import deque

# Ring buffer capacity — small on purpose. wrap_run only needs "what was the
# scraper complaining about right before it gave up", not a full transcript.
_BUFFER_CAPACITY = 10

_LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"


class RingBufferHandler(logging.Handler):
    """Keeps the last N WARNING-and-above log lines, oldest first.

    Attach to the root logger for the duration of a run, read ``tail()`` for
    a diagnostic once the run is over, then detach. Bounded via a
    ``deque(maxlen=...)`` so a chatty failure can't grow this without limit.
    """

    def __init__(self, capacity: int = _BUFFER_CAPACITY) -> None:
        super().__init__(level=logging.WARNING)
        self.setFormatter(logging.Formatter(_LOG_FORMAT))
        self.records: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:  # noqa: BLE001 — a broken record must never break logging
            pass

    def tail(self) -> str:
        """Join the captured lines, newest last. Empty string if none captured."""
        return "\n".join(self.records)


# Order matters — each pattern runs on the OUTPUT of the previous one, so a
# later pattern must not re-expose what an earlier one already collapsed to
# a placeholder. See module docstring for why this must err aggressive.

# Full URL query strings: keep scheme + host + path, drop everything from "?".
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#\"']+)\?[^\s\"'>)]*")

# JWTs: three dot-separated base64url segments, header always starts "eyJ".
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")

# Values following an Authorization/apikey/token/key/secret/password marker,
# an optional "Bearer " prefix, case-insensitive marker matching.
_MARKER_RE = re.compile(
    r"(?i)\b(authorization|apikey|api[_-]?key|access[_-]?token|token|"
    r"secret|password|passwd|pwd|key)\s*[:=]\s*(?:Bearer\s+)?[^\s,;\"'\)]+"
)

# KEY=value pairs where KEY looks like an env secret name — catches
# SUPABASE_SERVICE_ROLE_KEY=... etc, which _MARKER_RE's \b can't see because
# "KEY" there isn't preceded by a word boundary (the "_" before it is \w).
_ENV_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Za-z_][A-Za-z0-9_]*(?:key|token|secret|password)[A-Za-z0-9_]*)"
    r"\s*=\s*[^\s,;\"'\)]+"
)

# Catch-all: any remaining standalone hex/base64-looking run of 20+ chars.
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_=-]{20,}")


def scrub_secrets(text: str) -> str:
    """Redact likely secrets from ``text`` before it reaches PUBLIC run_logs.

    Redacts, in order: full URL query strings (scheme+host+path kept), JWTs,
    values after an Authorization/apikey/token/key/secret/password marker,
    KEY=value pairs whose KEY looks like an env secret name, and finally any
    remaining standalone hex/base64-looking run of 20+ characters.
    """
    text = _URL_QUERY_RE.sub(r"\1", text)
    text = _JWT_RE.sub("[REDACTED]", text)
    text = _MARKER_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _ENV_SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _LONG_TOKEN_RE.sub("[REDACTED]", text)
    return text
