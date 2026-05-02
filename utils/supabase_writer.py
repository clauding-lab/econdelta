"""Upsert numeric values from EconDelta's snapshot dict into Supabase
``metric_history`` — the brief's read-only history backend.

Architecture: EconDelta is the single source of truth and the single
writer. The brief consumes this table read-only via PostgREST. See
``docs/architecture-supabase.md`` for the full picture.

Schema assumed:
    metric_history (
        metric_id  text   primary key part 1
        as_of      date   primary key part 2
        value      numeric
        source     text
        ingested_at timestamptz default now()
    )
    on conflict (metric_id, as_of) do update.

Failure semantics: best-effort. ``upsert_metric_history`` raises
``SupabaseWriteError`` on network or auth failure; the caller (
``aggregate_latest.main``) logs and continues — the local
``data/archive/<date>.json`` is the cold backup, and the next aggregate
retry retransmits the same rows (idempotent on (metric_id, as_of)).
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Mapping

import requests

logger = logging.getLogger("supabase_writer")

# How many rows to send in one POST. PostgREST is comfortable with a few
# hundred rows; we have ~60+ keys per snapshot so one batch suffices.
_BATCH_SIZE = 500
_DEFAULT_TIMEOUT = 30
_DEFAULT_SOURCE = "EconDelta"


class SupabaseWriteError(Exception):
    """Raised when the Supabase upsert fails fatally."""


def _resolve_credentials(
    url: str | None, service_key: str | None,
) -> tuple[str, str]:
    resolved_url = url or os.environ.get("SUPABASE_URL")
    resolved_key = (
        service_key
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SERVICE_KEY")
    )
    if not resolved_url:
        raise SupabaseWriteError("SUPABASE_URL not set in env or kwargs")
    if not resolved_key:
        raise SupabaseWriteError(
            "SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SERVICE_KEY) not set in env or kwargs"
        )
    return resolved_url.rstrip("/"), resolved_key


def _rows_from_data(
    data: Mapping[str, object], as_of: date, source: str,
) -> list[dict]:
    rows: list[dict] = []
    for metric_id, value in data.items():
        if isinstance(value, bool):
            # `bool` is a subclass of `int` in Python — exclude explicitly so
            # any ``status: true``-style flag in the snapshot doesn't slip in.
            continue
        if isinstance(value, (int, float)):
            rows.append({
                "metric_id": metric_id,
                "as_of": as_of.isoformat(),
                "value": value,
                "source": source,
            })
    return rows


def upsert_metric_history(
    *,
    data: Mapping[str, object],
    as_of: date,
    source: str = _DEFAULT_SOURCE,
    url: str | None = None,
    service_key: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> int:
    """Upsert every numeric value in ``data`` to ``metric_history``.

    Args:
        data: The flat snapshot dict (typically ``latest.json["data"]``).
        as_of: The date these readings should be stored under (typically today).
        source: Default "EconDelta"; per-row override not supported (one
                aggregator run = one source label).
        url, service_key: Override for SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY
                env vars. Tests pass these directly.
        timeout: Per-request timeout seconds.
        session: Override for tests — pass a mock with ``.post(...)`` matching
                 ``requests.Session.post``.

    Returns:
        Count of rows upserted.

    Raises:
        SupabaseWriteError: On missing creds, network failure, or non-2xx
            response. Caller decides whether to abort or continue.
    """
    base_url, key = _resolve_credentials(url, service_key)
    rows = _rows_from_data(data, as_of, source)
    if not rows:
        logger.info("no scalar values to upsert (snapshot empty or non-numeric only)")
        return 0

    endpoint = f"{base_url}/rest/v1/metric_history?on_conflict=metric_id,as_of"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    sess = session or requests.Session()

    upserted = 0
    for start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[start:start + _BATCH_SIZE]
        try:
            resp = sess.post(endpoint, json=batch, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise SupabaseWriteError(f"network error during upsert: {e}") from e
        if resp.status_code not in (200, 201, 204):
            raise SupabaseWriteError(
                f"upsert returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        upserted += len(batch)

    return upserted
