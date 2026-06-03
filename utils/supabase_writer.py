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
from typing import Callable as _Callable
from typing import Mapping
from typing import Optional as _Optional

import requests

logger = logging.getLogger("supabase_writer")

# How many rows to send in one POST. PostgREST is comfortable with a few
# hundred rows; we have ~60+ keys per snapshot so one batch suffices.
_BATCH_SIZE = 500
_DEFAULT_TIMEOUT = 30
_DEFAULT_SOURCE = "EconDelta"

# Keys in ``data`` that are by-design metadata, not numeric history rows.
# Skipped silently — the writer's non-scalar warning (below) is reserved for
# genuinely unexpected shapes. Update this set when adding a new metadata key
# in ``aggregate_latest.py`` (search for ``data[`` assignments returning
# non-numeric values). Tests: ``tests/test_supabase_writer.py``.
#
#   reserves_date           — ISO date string from bb_forex.reserves
#   trading_day             — date label string from dse_market
#   nbr_fytd_cross_check    — provenance tag ("single_source_tax_revenue")
#   commodity_change_pct    — dict of {commodity_key: pct}; per-commodity
#                             prices are already in ``data`` as scalars
_KNOWN_NON_HISTORY_KEYS = frozenset({
    "reserves_date",
    "trading_day",
    "nbr_fytd_cross_check",
    "commodity_change_pct",
})


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
        if metric_id in _KNOWN_NON_HISTORY_KEYS:
            # By-design metadata key — never a numeric history row.
            continue
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
        else:
            # Genuinely unexpected non-scalar shape (dict, list, str, None, ...)
            # for a key that ISN'T in ``_KNOWN_NON_HISTORY_KEYS``. PR #31 traced
            # months of zero rows for ``call_money_rate`` to a dict-shaped parser
            # output landing here. Warn so the next shape mismatch surfaces on
            # the first fire, not in a weekly review. Proper fix is either to
            # add a flatten rule in ``aggregate_latest._flatten_dict_indicators``
            # (for numeric series fan-out) or to add the key to
            # ``_KNOWN_NON_HISTORY_KEYS`` above (for genuine metadata).
            logger.warning(
                "supabase_writer: dropping non-scalar value for metric_id=%s (type=%s)",
                metric_id, type(value).__name__,
            )
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
# Structured row-table writer — auction_results / auction_calendar (S8)
# ----------------------------------------------------------------------------
# metric_history is scalar-numeric-only and ``_rows_from_data`` keeps only
# int/float — it CANNOT store a per-print auction row (multi-field) or a
# forward-calendar row. These tables (supabase/migrations/0009_auction_results.sql)
# hold the row-shaped data; this path POSTs whole rows, not flattened scalars.
# Generic enough to serve both tables via the two thin wrappers below.
# ============================================================================

# Allowed columns per row-table — guards against a typo'd or stray key landing
# in PostgREST (which would 400 the whole batch). The two PK columns are
# always required; the rest are optional (a calendar row has no cover/wam, an
# un-priced result field may be null).
_AUCTION_RESULTS_COLUMNS = frozenset(
    {"auction_date", "tenor", "size", "bid", "cover", "wam", "cutoff", "ingested_at"}
)
_AUCTION_CALENDAR_COLUMNS = frozenset(
    {"auction_date", "tenor", "notional", "ingested_at"}
)
_AUCTION_PK = ("auction_date", "tenor")


def _validate_auction_rows(
    rows: list[Mapping[str, object]], allowed_columns: frozenset[str],
) -> list[dict]:
    """Validate + normalise row-table rows before POST.

    Every row MUST carry both PK fields (auction_date, tenor); auction_date
    is normalised to an ISO string if a ``date`` was passed. Unknown columns
    are rejected (a stray key would 400 the whole PostgREST batch and is
    almost certainly a caller bug, not data to silently drop).

    Raises:
        ValueError: missing PK field, or a column not in ``allowed_columns``.
    """
    out: list[dict] = []
    for i, row in enumerate(rows):
        for pk in _AUCTION_PK:
            if row.get(pk) is None:
                raise ValueError(
                    f"auction row {i} missing required primary-key field {pk!r}"
                )
        unknown = set(row) - allowed_columns
        if unknown:
            raise ValueError(
                f"auction row {i} has unknown column(s) {sorted(unknown)}; "
                f"allowed: {sorted(allowed_columns)}"
            )
        normalised = dict(row)
        ad = normalised["auction_date"]
        if isinstance(ad, date):
            normalised["auction_date"] = ad.isoformat()
        out.append(normalised)

    # PostgREST bulk-upsert (PGRST102 "All object keys must match") requires every
    # object in the batch to carry the SAME keys. Result rows are heterogeneous —
    # bond rows have `wam`, bills don't — so reconcile to the union of keys present,
    # filling a missing column with None (a genuine SQL NULL, not a fabricated value).
    all_keys: set[str] = set().union(*(r.keys() for r in out)) if out else set()
    for r in out:
        for k in all_keys:
            r.setdefault(k, None)
    return out


def upsert_auction_rows(
    rows: list[Mapping[str, object]],
    *,
    table: str,
    allowed_columns: frozenset[str],
    url: str | None = None,
    service_key: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> int:
    """Upsert row-shaped data into a structured table on (auction_date, tenor).

    Generic over the two auction tables; ``upsert_auction_results`` and
    ``upsert_auction_calendar`` are the thin wrappers callers use.

    Args:
        rows: List of row dicts. Each MUST carry ``auction_date`` (date or ISO
            string) and ``tenor``; other columns must be in ``allowed_columns``.
        table: Target table name ('auction_results' or 'auction_calendar').
        allowed_columns: The table's column allow-list (PK + optional fields).
        url, service_key: Override for SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY.
        timeout: Per-request timeout seconds.
        session: Override for tests — a mock with ``.post(...)``.

    Returns:
        Count of rows upserted (0 if ``rows`` is empty).

    Raises:
        ValueError: A row is missing a PK field or carries an unknown column.
        SupabaseWriteError: On missing creds, network failure, or non-2xx.
    """
    if not rows:
        logger.info("upsert_auction_rows: no rows to upsert for table=%s", table)
        return 0

    validated = _validate_auction_rows(rows, allowed_columns)
    base_url, key = _resolve_credentials(url, service_key)
    conflict = ",".join(_AUCTION_PK)
    endpoint = f"{base_url}/rest/v1/{table}?on_conflict={conflict}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    sess = session or requests.Session()

    upserted = 0
    for start in range(0, len(validated), _BATCH_SIZE):
        batch = validated[start:start + _BATCH_SIZE]
        try:
            resp = sess.post(endpoint, json=batch, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise SupabaseWriteError(
                f"network error during {table} upsert: {e}"
            ) from e
        if resp.status_code not in (200, 201, 204):
            raise SupabaseWriteError(
                f"{table} upsert returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        upserted += len(batch)

    return upserted


def upsert_auction_results(
    rows: list[Mapping[str, object]], **kwargs,
) -> int:
    """Upsert per-print RESULTS rows into ``auction_results`` on (auction_date, tenor).

    Each row: ``{auction_date, tenor, size?, bid?, cover?, wam?, cutoff?}``.
    """
    return upsert_auction_rows(
        rows,
        table="auction_results",
        allowed_columns=_AUCTION_RESULTS_COLUMNS,
        **kwargs,
    )


def upsert_auction_calendar(
    rows: list[Mapping[str, object]], **kwargs,
) -> int:
    """Upsert forward-calendar rows into ``auction_calendar`` on (auction_date, tenor).

    Each row: ``{auction_date, tenor, notional?}`` — NO bid/cover/wam/cutoff
    (those don't exist for an un-held auction).
    """
    return upsert_auction_rows(
        rows,
        table="auction_calendar",
        allowed_columns=_AUCTION_CALENDAR_COLUMNS,
        **kwargs,
    )


def upsert_briefing(row, *, url=None, service_key=None, timeout=_DEFAULT_TIMEOUT, session=None):
    """Upsert one weekly briefing row (PK week_of). Raises SupabaseWriteError on failure.

    Unlike run_logs helpers (which swallow errors), this RAISES — a failed
    briefing write must be visible so the job returns non-zero.
    """
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") == "1":
        return
    base_url, key = _resolve_credentials(url, service_key)
    endpoint = f"{base_url}/rest/v1/briefings?on_conflict=week_of"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    sess = session or requests.Session()
    try:
        resp = sess.post(endpoint, json=row, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise SupabaseWriteError(f"briefing upsert network error: {e}") from e
    if resp.status_code not in (200, 201, 204):
        raise SupabaseWriteError(f"briefing upsert returned HTTP {resp.status_code}: {resp.text[:200]}")


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


# ============================================================================
# Metric definitions seed helper — idempotent ON CONFLICT DO NOTHING upsert
# ============================================================================

_DEFAULT_DEFINITION_FIELDS = {
    "short_label": None,
    "unit": None,
    "sort_order": 100,
    "cadence": None,
    "format": "comma-2dp",
    "description": None,
    "source": None,
    "source_url": None,
    "is_hero": False,
    "inverted": False,
}


def _normalize_definition(d: dict) -> dict:
    """Validate required fields, fill defaults, return upsert-ready row."""
    if "metric_id" not in d:
        raise KeyError("definition missing required field 'metric_id'")
    if "label" not in d:
        raise KeyError("definition missing required field 'label'")
    if "domain" not in d:
        raise KeyError("definition missing required field 'domain'")
    out = {**_DEFAULT_DEFINITION_FIELDS, **d}
    return out


def insert_media_review_rows(candidates, *, url=None, service_key=None,
                             timeout=_DEFAULT_TIMEOUT, session=None) -> int:
    """Insert review Candidates as status='pending' rows into media_review.

    Returns count inserted (0 if empty). Raises SupabaseWriteError on non-2xx.
    """
    if not candidates:
        return 0
    base_url, key = _resolve_credentials(url, service_key)
    rows = [{
        "metric_id": c.metric_id,
        "parsed_value": c.parsed_value,
        "parsed_as_of": c.parsed_as_of.isoformat() if c.parsed_as_of else None,
        "press_value": c.press_value,
        "press_as_of": c.press_as_of.isoformat(),
        "kind": c.kind,
        "source_outlet": c.source_outlet,
        "source_url": c.source_url,
        "source_quote": c.source_quote,
        "confidence": c.confidence,
        "status": "pending",
    } for c in candidates]
    endpoint = f"{base_url}/rest/v1/media_review"
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    sess = session or requests.Session()
    try:
        resp = sess.post(endpoint, json=rows, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(f"media_review insert network error: {e}") from e
    if resp.status_code not in (200, 201, 204):
        raise SupabaseWriteError(f"media_review insert HTTP {resp.status_code}: {resp.text[:200]}")
    return len(rows)


def set_media_review_status(review_id, status, *, applied: bool = False,
                            url=None, service_key=None, timeout=_DEFAULT_TIMEOUT, session=None) -> None:
    """PATCH one media_review row's status (+ applied_at when applied=True).
    Raises SupabaseWriteError on non-2xx."""
    base_url, key = _resolve_credentials(url, service_key)
    payload: dict = {"status": status}
    if applied:
        from datetime import datetime, timezone
        payload["applied_at"] = datetime.now(timezone.utc).isoformat()
    endpoint = f"{base_url}/rest/v1/media_review?id=eq.{int(review_id)}"
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    sess = session or requests.Session()
    try:
        resp = sess.patch(endpoint, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(f"media_review status patch network error: {e}") from e
    if resp.status_code not in (200, 204):
        raise SupabaseWriteError(f"media_review status patch HTTP {resp.status_code}: {resp.text[:200]}")


def upsert_metric_definitions_seed(definitions: list[dict]) -> int:
    """Insert metric_definitions rows with ON CONFLICT (metric_id) DO NOTHING.

    First insert wins forever; manual edits in Supabase Studio are preserved.
    Returns count of NEW rows inserted (0 in test/skip mode).

    Raises KeyError for definitions missing required fields (metric_id, label, domain).
    """
    if not definitions:
        return 0

    rows = [_normalize_definition(d) for d in definitions]

    if os.environ.get("ECONDELTA_SKIP_SUPABASE") == "1":
        return 0

    try:
        base_url, key = _resolve_credentials(None, None)
        endpoint = f"{base_url}/rest/v1/metric_definitions?on_conflict=metric_id"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # ignore-duplicates = ON CONFLICT DO NOTHING; return=representation
            # means PostgREST returns only the actually-inserted (new) rows.
            "Prefer": "resolution=ignore-duplicates,return=representation",
        }
        sess = requests.Session()
        resp = sess.post(endpoint, json=rows, headers=headers, timeout=_DEFAULT_TIMEOUT)
        if resp.status_code not in (200, 201, 204):
            logger.error(
                "upsert_metric_definitions_seed returned HTTP %s: %s",
                resp.status_code,
                resp.text[:200],
            )
            raise SupabaseWriteError(
                f"upsert_metric_definitions_seed returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        # With return=representation + ignore-duplicates, PostgREST returns
        # only the rows that were actually inserted (new rows). Existing rows
        # return as empty []. len() gives the new-row count.
        try:
            inserted = resp.json()
            return len(inserted) if isinstance(inserted, list) else 0
        except Exception:
            return 0
    except SupabaseWriteError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("upsert_metric_definitions_seed failed: %s", e)
        raise
