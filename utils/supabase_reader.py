"""PostgREST GET helpers for the briefing job.

The repo has no Supabase SELECT helper (utils/supabase_writer.py is POST/PATCH
only; opus_review.load_history reads LOCAL archive JSON). This module adds the
read side, mirroring the writer's style: reuse _resolve_credentials, accept a
session= for injection, and RAISE on failure (reads are load-bearing for the
briefing — unlike run_logs writes, they must not silently degrade).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from utils.supabase_writer import _resolve_credentials

_DEFAULT_TIMEOUT = 30


class SupabaseReadError(RuntimeError):
    """Raised when a PostgREST GET fails or returns non-2xx."""


def _get(path: str, *, url: str | None, key: str | None,
         session: requests.Session | None, timeout: int = _DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    base_url, resolved_key = _resolve_credentials(url, key)
    endpoint = f"{base_url}/rest/v1/{path}"
    headers = {"apikey": resolved_key, "Authorization": f"Bearer {resolved_key}"}
    sess = session or requests.Session()
    try:
        resp = sess.get(endpoint, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise SupabaseReadError(f"GET {path} network error: {e}") from e
    if resp.status_code not in (200, 206):
        raise SupabaseReadError(f"GET {path} returned HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def get_metric_history(metric_id: str, *, days: int, url: str | None = None,
                       key: str | None = None, session: requests.Session | None = None) -> list[dict[str, Any]]:
    """Most-recent `days` rows for one metric, newest first."""
    path = f"metric_history?metric_id=eq.{metric_id}&order=as_of.desc&limit={days}"
    return _get(path, url=url, key=key, session=session)


def get_recent_run_ok(source: str, *, within_hours: int, url: str | None = None,
                      key: str | None = None, session: requests.Session | None = None) -> bool:
    """True if the latest run_logs row for `source` with status='ok' started within the window.

    This is the anti-carry-forward signal: a fresh as_of can hide a dead parse,
    but a recent successful aggregate run cannot be faked.
    """
    path = f"run_logs?source=eq.{source}&status=eq.ok&order=started_at.desc&limit=1"
    rows = _get(path, url=url, key=key, session=session)
    if not rows:
        return False
    started = datetime.fromisoformat(rows[0]["started_at"])
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
    return age_hours <= within_hours


def get_recent_briefings(*, limit: int, url: str | None = None, key: str | None = None,
                         session: requests.Session | None = None) -> list[dict[str, Any]]:
    """The last `limit` briefings, newest first (for prompt context + open_threads)."""
    path = f"briefings?order=week_of.desc&limit={limit}"
    return _get(path, url=url, key=key, session=session)
