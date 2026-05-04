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
import uuid as _uuid
from datetime import date, datetime, timezone
from typing import Callable as _Callable, Mapping, Optional as _Optional

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
    data: Mapping[str, object],
    as_of: date,
    source: str,
    source_as_of_map: Mapping[str, date] | None = None,
) -> list[dict]:
    """Build PostgREST row dicts from ``data``.

    Args:
        data: Flat snapshot dict — only ``int`` and ``float`` values are kept.
        as_of: Global fallback date for all metrics without a per-metric override.
        source: Source label written to ``metric_history.source``.
        source_as_of_map: Optional per-metric publication-date overrides. When a
            metric_id appears in this map, that date is used as ``as_of`` instead
            of the global fallback. This is the key fix for the as_of bug: quarterly
            BB FSAR metrics (banking_npl_pct, banking_car_pct) supply the quarter-end
            date (e.g. 2025-09-30) rather than today's run date.
    """
    rows: list[dict] = []
    overrides = source_as_of_map or {}
    for metric_id, value in data.items():
        if isinstance(value, bool):
            # `bool` is a subclass of `int` in Python — exclude explicitly so
            # any ``status: true``-style flag in the snapshot doesn't slip in.
            continue
        if isinstance(value, (int, float)):
            effective_as_of = overrides.get(metric_id, as_of)
            rows.append({
                "metric_id": metric_id,
                "as_of": effective_as_of.isoformat(),
                "value": value,
                "source": source,
            })
    return rows


def upsert_metric_history(
    *,
    data: Mapping[str, object],
    as_of: date,
    source: str = _DEFAULT_SOURCE,
    source_as_of_map: Mapping[str, date] | None = None,
    url: str | None = None,
    service_key: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> int:
    """Upsert every numeric value in ``data`` to ``metric_history``.

    Args:
        data: The flat snapshot dict (typically ``latest.json["data"]``).
        as_of: The date these readings should be stored under (typically today).
            This is the global fallback — use ``source_as_of_map`` to override
            per-metric dates for slow-cadence sources (quarterly FSAR, monthly
            news articles).
        source: Default "EconDelta"; per-row override not supported (one
                aggregator run = one source label).
        source_as_of_map: Optional mapping of metric_id → true publication date.
            Overrides ``as_of`` for those specific metrics. Metrics absent from
            this map use the global ``as_of`` fallback. Pass None (default) for
            backward compatibility.
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
    rows = _rows_from_data(data, as_of, source, source_as_of_map)
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


# ============================================================================
# Run logging helpers — write to public.run_logs for the PWA Runs page
# ============================================================================

_RUN_LOGS_TIMEOUT = 10  # short timeout; logging must not block scrapers


def log_run_start(
    source: str,
    unit: _Optional[str] = None,
    started_at: _Optional[datetime] = None,
) -> str:
    """Insert a starting row in run_logs, return uuid for matching log_run_end().

    Swallows network errors — a logging failure must not mask the scrape outcome.
    Returns a local uuid even on failure so log_run_end() has something to update
    (the update will also be a no-op).
    """
    run_id = str(_uuid.uuid4())
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") == "1":
        return run_id

    if started_at is None:
        started_at = datetime.now(timezone.utc)

    try:
        base_url, key = _resolve_credentials(None, None)
        import socket as _socket
        host = os.environ.get("ECONDELTA_HOST", _socket.gethostname())
        endpoint = f"{base_url}/rest/v1/run_logs"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        payload = {
            "id": run_id,
            "source": source,
            "started_at": started_at.isoformat(),
            "status": "running",
            "host": host,
            "unit": unit,
        }
        sess = requests.Session()
        sess.post(endpoint, json=payload, headers=headers, timeout=_RUN_LOGS_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — by design, we swallow logging errors
        logger.warning("log_run_start failed for source=%s: %s", source, e)

    return run_id


def log_run_end(
    run_id: str,
    started_at: datetime,
    status: str,
    exit_code: int = 0,
    error: _Optional[str] = None,
) -> None:
    """Update a run_logs row with finished_at, duration_ms, status, exit_code, error.

    Swallows network errors. Status must be one of: 'ok', 'fail', 'stale', 'skip'.
    """
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") == "1":
        return

    finished_at = datetime.now(timezone.utc)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)

    try:
        base_url, key = _resolve_credentials(None, None)
        endpoint = f"{base_url}/rest/v1/run_logs?id=eq.{run_id}"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        payload = {
            "finished_at": finished_at.isoformat(),
            "duration_ms": duration_ms,
            "status": status,
            "exit_code": exit_code,
            "error": error[:2000] if error else None,  # truncate long tracebacks
        }
        sess = requests.Session()
        sess.patch(endpoint, json=payload, headers=headers, timeout=_RUN_LOGS_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        logger.warning("log_run_end failed for run_id=%s: %s", run_id, e)


_STATUS_BY_EXIT = {0: "ok", 1: "fail", 2: "stale", 3: "skip"}


def wrap_run(source: str, unit: str, main_func: _Callable[[], int]) -> int:
    """Wrap a scraper's main() with run_logs instrumentation.

    Pattern at scraper bottom:
        if __name__ == '__main__':
            sys.exit(wrap_run('bb_forex', 'econdelta-forex.service', main))

    Maps main()'s exit code to run_logs.status:
        0 -> 'ok', 1 -> 'fail', 2 -> 'stale', 3 -> 'skip', other -> 'fail'
    Uncaught exceptions are logged as 'fail' with error=type(e).__name__: str(e),
    then re-raised so systemd records non-zero exit.
    """
    started_at = datetime.now(timezone.utc)
    run_id = log_run_start(source=source, unit=unit, started_at=started_at)
    try:
        exit_code = main_func()
        status = _STATUS_BY_EXIT.get(exit_code, "fail")
        log_run_end(run_id, started_at, status=status, exit_code=exit_code)
        return exit_code
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log_run_end(run_id, started_at, status="fail", exit_code=1, error=err)
        raise
