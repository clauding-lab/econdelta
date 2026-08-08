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
        provenance text   nullable, migration 0013 — see _provenance_enabled()
    )
    on conflict (metric_id, as_of) do update.

``provenance`` (extraction method: 'deterministic' | 'llm' | 'hybrid' |
'manual') is a DIFFERENT concept from ``source`` (originating organization) —
never conflate them. It's written only when a caller passes ``provenance=``
AND ``ECONDELTA_PROVENANCE_ENABLED=1`` is set, because the column does not
exist in the live DB until the owner applies
``supabase/migrations/0013_provenance.sql`` (see that file's header for the
two supported apply routes).

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

from utils.run_log_capture import RingBufferHandler, scrub_secrets

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
#   nbr_fytd_cross_check    — source tag ("single_source_tax_revenue")
#   commodity_change_pct    — dict of {commodity_key: pct}; per-commodity
#                             prices are already in ``data`` as scalars
_KNOWN_NON_HISTORY_KEYS = frozenset({
    "reserves_date",
    "trading_day",
    "nbr_fytd_cross_check",
    "commodity_change_pct",
})

# ----------------------------------------------------------------------------
# Provenance (extraction method) — see supabase/migrations/0013_provenance.sql.
#
# `provenance` records HOW a value was pulled out of its source document
# ('deterministic' regex/table parser | 'llm' Claude extraction | 'hybrid'
# deterministic-parse-plus-LLM-recovered-field | 'manual' hand-transcribed).
# This is NOT `source`, which records the ORIGINATING ORGANIZATION — never
# conflate the two.
#
# The column does not exist in the live DB until the owner applies migration
# 0013 — via `supabase db query --linked -f` (db/README.md's canonical route)
# or a Supabase dashboard SQL-editor paste (see that migration file's header
# for both routes). Until then, a payload carrying a `provenance` key would
# 400 the WHOLE batch (PostgREST rejects an unrecognised column on every row,
# not just its own).
# So this is gated behind an explicit env flag that defaults OFF:
#   - merge-safe BEFORE the DDL lands (flag unset -> key never sent, writes
#     keep working against the pre-migration schema)
#   - opt-in AFTER the DDL lands (owner sets ECONDELTA_PROVENANCE_ENABLED=1
#     in /etc/econdelta.env on the box once the migration is confirmed
#     applied — see that file's VERIFICATION block)
# A caller passing `provenance=` while the flag is unset is a no-op, not an
# error: the column simply isn't populated yet.
_VALID_PROVENANCE = frozenset({"deterministic", "llm", "hybrid", "manual"})
_PROVENANCE_ENV_FLAG = "ECONDELTA_PROVENANCE_ENABLED"


def _provenance_enabled() -> bool:
    """True once the owner has applied migration 0013 and flipped the flag."""
    return os.environ.get(_PROVENANCE_ENV_FLAG) == "1"


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
    ingested_at: datetime | None = None,
    provenance: str | None = None,
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
        ingested_at: The write timestamp posted on EVERY row so a merge-upsert
            (ON CONFLICT DO UPDATE) BUMPS ``metric_history.ingested_at``. Defaults
            to now (UTC). See below for why this matters.
        provenance: Optional extraction-method tag — one of 'deterministic',
            'llm', 'hybrid', 'manual' (see supabase/migrations/0013_provenance.sql).
            Included in every row's payload ONLY when this is not None AND
            ECONDELTA_PROVENANCE_ENABLED=1 is set in the environment; otherwise
            the key is omitted entirely so the payload stays compatible with a
            pre-migration schema (the column may not exist in the live DB yet).
            Raises ValueError if set to anything outside the four allowed values.

    ``ingested_at`` is posted explicitly (not left to the column's ``default now()``)
    because that default only fires on INSERT, never on the UPDATE half of the
    merge-upsert. A slow-cadence metric whose ``as_of`` is legitimately pinned to a
    recovered reporting vintage (e.g. ``debt_domestic_stock_cr`` → 2025-12-31) is
    re-written to the SAME (metric_id, as_of) row every run: its ``value`` updates
    in place but, without posting ``ingested_at``, its write timestamp froze at the
    row's first insert. Result: a pipeline that is writing daily reads as "last seen
    weeks ago" to any freshness check keyed on ``ingested_at`` — the E1.1 freeze.
    Posting a fresh ``ingested_at`` each run makes write-liveness observable even
    when ``as_of`` correctly stalls. (E1.1)
    """
    if provenance is not None and provenance not in _VALID_PROVENANCE:
        raise ValueError(
            f"provenance must be one of {sorted(_VALID_PROVENANCE)}, got {provenance!r}"
        )
    include_provenance = provenance is not None and _provenance_enabled()

    rows: list[dict] = []
    overrides = source_as_of_map or {}
    stamp = (ingested_at or datetime.now(timezone.utc)).isoformat()
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
            row = {
                "metric_id": metric_id,
                "as_of": effective_as_of.isoformat(),
                "value": value,
                "source": source,
                "ingested_at": stamp,
            }
            if include_provenance:
                row["provenance"] = provenance
            rows.append(row)
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
    ingested_at: datetime | None = None,
    provenance: str | None = None,
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
                aggregator run = one source label). Records the ORIGINATING
                ORGANIZATION — do not conflate with ``provenance`` below.
        source_as_of_map: Optional mapping of metric_id → true publication date.
            Overrides ``as_of`` for those specific metrics. Metrics absent from
            this map use the global ``as_of`` fallback. Pass None (default) for
            backward compatibility.
        ingested_at: Explicit write timestamp posted on every row. Pass the same
            value to ``verify_landed_count(since=...)`` so the post-write read-back
            counts exactly this run's rows (E2.2). Defaults to now (UTC).
        provenance: Optional extraction-method tag — one of 'deterministic',
            'llm', 'hybrid', 'manual'. Records HOW the value was pulled out of
            its source document (never the organization — that's ``source``).
            One call = one provenance label for every row in this batch, same
            as ``source``. Only actually written to Supabase when
            ECONDELTA_PROVENANCE_ENABLED=1 is ALSO set — see
            ``supabase/migrations/0013_provenance.sql`` and
            ``_provenance_enabled()`` for why the flag exists. Leave unset
            (None, the default) when a call site's extraction method is mixed
            or ambiguous rather than guessing.
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
        ValueError: ``provenance`` is set to something other than one of the
            four allowed values.
    """
    base_url, key = _resolve_credentials(url, service_key)
    rows = _rows_from_data(data, as_of, source, source_as_of_map, ingested_at, provenance)
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


def verify_landed_count(
    expected: int,
    *,
    since: datetime,
    metric_ids: "list[str] | None" = None,
    table: str = "metric_history",
    source_label: str = "",
    url: str | None = None,
    service_key: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> bool | None:
    """Read-back guard: confirm ``expected`` rows actually landed; alert if not.

    The enforced invariant landmine 22 never got. A 2xx response / a "wrote N
    rows" log is NOT proof of persistence: a misrouted ``url=`` override 2xx'd to
    the source host while nothing landed (E1.5 / Tier-2 class), and a frozen
    merge-upsert re-wrote the same row daily. After an upsert made with an
    explicit ``ingested_at=since``, this counts the rows now carrying
    ``ingested_at >= since`` and compares to ``expected``. Mismatch →
    ``notify('error')``.

    Args:
        expected: rows the upsert reported writing (its return value).
        since: the SAME timestamp passed as ``upsert_metric_history(ingested_at=)``.
        metric_ids: scope the count to these ids. The aggregate (sole writer in
            its 07:00 window) can leave this None; a direct writer that shares a
            fire window with siblings (the 23:xx cascade) passes its own ids so a
            neighbouring writer's rows can't inflate the count.
        source_label: label for the log/alert (e.g. "aggregate", "pink_sheet").

    Returns:
        True if the landed count matches, False on mismatch (after alerting),
        None if it couldn't verify (skipped in tests, or the read failed —
        verification must never crash or block the writer).
    """
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") == "1":
        return None
    label = source_label or table
    try:
        base_url, key = _resolve_credentials(url, service_key)
        stamp = since.isoformat() if isinstance(since, datetime) else str(since)
        params = {"select": "metric_id", "ingested_at": f"gte.{stamp}"}
        if metric_ids:
            params["metric_id"] = "in.(" + ",".join(sorted(set(metric_ids))) + ")"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            # count=exact returns the true total in Content-Range regardless of
            # the page-size cap, and Range 0-0 transfers ~1 row, not thousands.
            "Prefer": "count=exact",
            "Range-Unit": "items",
            "Range": "0-0",
        }
        endpoint = f"{base_url}/rest/v1/{table}"
        sess = session or requests.Session()
        resp = sess.get(endpoint, params=params, headers=headers, timeout=timeout)
        if resp.status_code not in (200, 206):
            logger.warning(
                "verify_landed_count[%s]: read HTTP %s: %s",
                label, resp.status_code, resp.text[:200],
            )
            return None
        landed = _parse_content_range_total(resp)
    except Exception as e:  # noqa: BLE001 — verification must not crash the writer
        logger.warning("verify_landed_count[%s]: read failed: %s", label, e)
        return None

    if landed == expected:
        logger.info("verify_landed_count[%s]: %d row(s) landed as expected", label, landed)
        return True

    notify = _lazy_notify()
    notify(
        "error",
        "landed-count mismatch",
        f"{label}: expected {expected} metric_history row(s) with ingested_at>={stamp} "
        f"but found {landed}. A 2xx write is NOT proof of persistence (landmine 22) — "
        f"check for a misrouted write (url= override), a frozen merge-upsert, or a "
        f"dropped/partial batch.",
    )
    return False


def _parse_content_range_total(resp: requests.Response) -> int:
    """Extract the exact row total from a PostgREST ``count=exact`` response.

    Content-Range looks like ``0-0/1234`` (or ``*/1234`` when the range is empty).
    Falls back to counting the returned body if the header is missing/unparseable.
    """
    cr = resp.headers.get("Content-Range", "")
    if "/" in cr:
        total = cr.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    try:
        body = resp.json()
        return len(body) if isinstance(body, list) else 0
    except Exception:  # noqa: BLE001
        return 0


def _lazy_notify():
    """Import notify lazily so the writer's import graph stays minimal."""
    from utils.notifier import notify
    return notify


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

# run_logs.error is capped well below log_run_end's own 2000-char truncate —
# this is a short diagnostic pointer, not a transcript.
_RUN_ERROR_MAX_CHARS = 500

# Absolute ceiling on the text handed to scrub_secrets, applied BEFORE
# scrubbing. Several of its patterns (_ENV_SECRET_ASSIGN_RE in particular)
# backtrack super-linearly; without a cap here, a pathological multi-megabyte
# record (e.g. an exception whose str(e) embeds a full HTML error page) would
# turn the scrub itself into a resource hazard. Comfortably larger than any
# real ring-buffer tail (10 records) or exception message this codebase
# currently produces, so it never affects normal output.
_PRE_SCRUB_CAP_CHARS = 5_000

# Fallback format for the stderr handler wrap_run installs when nothing else
# has configured logging yet. Matches the format string used by the majority
# of wrap_run's own callers (aggregate_latest, sentinel, and 10 of the 14
# scrapers that call logging.basicConfig() inside their own main()).
_DEFAULT_STDERR_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Statuses where a self-handled failure otherwise leaves error=null. 'skip'
# is a by-design no-op (landmine 48) and stays null on purpose.
_STATUSES_NEEDING_CAPTURED_ERROR = frozenset({"fail", "stale"})


def _finalize_run_error(tail: str, *, head: str | None = None) -> str:
    """Scrub, then truncate, the captured log tail (and optional head) before
    this reaches PUBLIC run_logs.

    Order matters: scrubbing MUST run before truncation. Three of
    ``scrub_secrets``'s five patterns are left-anchored (a URL needs its
    leading ``https?://``, a JWT needs its leading ``eyJ``, an env-secret
    assignment needs a word boundary before the marker) — truncating first
    can slice into the middle of any of them, silently defeating that
    pattern for whatever secret straddled the cut. ``tail`` and ``head`` are
    each capped at ``_PRE_SCRUB_CAP_CHARS`` and scrubbed independently
    (never concatenated first) so scrubbing itself stays bounded and a
    truncation can never land mid-pattern by cutting across the head/tail
    boundary either.

    ``head`` is the caller's most valuable string — an uncaught exception's
    own ``Type: message`` — and is preserved in full up to
    ``_RUN_ERROR_MAX_CHARS`` regardless of how much of the ring-buffer tail
    that leaves room for. Only ``tail`` is trimmed to fit, and only from its
    FRONT (oldest lines first) so the most recent captured log line — the
    one right before the run gave up — survives.
    """
    scrubbed_tail = scrub_secrets(tail[:_PRE_SCRUB_CAP_CHARS])
    if head is None:
        return scrubbed_tail[-_RUN_ERROR_MAX_CHARS:]

    scrubbed_head = scrub_secrets(head[:_PRE_SCRUB_CAP_CHARS])[:_RUN_ERROR_MAX_CHARS]
    budget = _RUN_ERROR_MAX_CHARS - len(scrubbed_head)
    separator = "\n"
    if budget <= len(separator) or not scrubbed_tail:
        return scrubbed_head
    return scrubbed_head + separator + scrubbed_tail[-(budget - len(separator)):]


def wrap_run(source: str, unit: str, main_func: _Callable[[], int]) -> int:
    """Wrap a scraper's main() with run_logs instrumentation.

    Pattern at scraper bottom:
        if __name__ == '__main__':
            sys.exit(wrap_run('bb_forex', 'econdelta-forex.service', main))

    Maps main()'s exit code to run_logs.status:
        0 -> 'ok', 1 -> 'fail', 2 -> 'stale', 3 -> 'skip', other -> 'fail'

    A ``RingBufferHandler`` is attached to the root logger for the duration
    of ``main_func`` so a self-handled failure (exit 1/2, no exception) still
    leaves a diagnostic: the last few WARNING-or-above log lines, scrubbed of
    secrets, become ``error=``. Uncaught exceptions keep the existing
    ``type(e).__name__: e`` string and append the same captured tail. The
    handler is always detached in ``finally``, including on raise.

    IMPORTANT: this attaches the RingBufferHandler to the root logger BEFORE
    ``main_func()`` runs. ``logging.basicConfig()`` is a documented no-op
    once ``root.handlers`` is non-empty, and every one of this function's 16
    entrypoints calls ``logging.basicConfig(level=logging.INFO, ...)`` as the
    first line of its own ``main()`` — so without the two steps below, that
    call would silently do nothing, root would stay at its default WARNING
    level, and ``logging.lastResort`` (which normally prints WARNING+ to
    stderr when nothing else is configured) would never fire either, because
    the RingBufferHandler itself counts as "something else is configured".
    Net effect: the on-disk systemd log files every unit appends stderr to
    (``deploy/econdelta-*.service``) would go silent. To prevent that, wrap_run
    reproduces what a fresh process's own ``basicConfig()`` call would have
    done, ahead of attaching the ring buffer:
      - if root has no handlers yet, install a stderr ``StreamHandler`` (the
        same thing ``basicConfig()`` would install) so INFO+ records keep
        reaching stderr the way they did before this ring buffer existed;
      - if root already has a handler (e.g. a test harness or an embedder
        pre-configured logging) but its level is coarser than INFO — which
        includes the interpreter's real un-configured default, WARNING —
        raise the level to INFO so that pre-existing handler keeps
        receiving INFO records too, without touching handlers the caller
        already owns. A caller that deliberately set something MORE
        permissive than INFO (e.g. DEBUG) is left alone.
    Each entrypoint's own ``basicConfig()`` call remains in place and is now
    a harmless no-op; it is not removed here.

    Not handled: nested ``wrap_run`` calls. Every real caller invokes this
    via ``python -m <module>`` (one wrap_run per process), so this isn't
    reachable today — but if a wrapped entrypoint ever called another
    wrapped ``main()`` in-process, both RingBufferHandlers would be live on
    root simultaneously and the inner run's warnings would also land in the
    outer run's captured ``error``.
    """
    started_at = datetime.now(timezone.utc)
    run_id = log_run_start(source=source, unit=unit, started_at=started_at)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=logging.INFO, format=_DEFAULT_STDERR_LOG_FORMAT)
    elif root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)
    handler = RingBufferHandler()
    root_logger.addHandler(handler)
    try:
        exit_code = main_func()
        status = _STATUS_BY_EXIT.get(exit_code, "fail")
        error = None
        if status in _STATUSES_NEEDING_CAPTURED_ERROR:
            tail = handler.tail()
            error = _finalize_run_error(tail) if tail else None
        log_run_end(run_id, started_at, status=status, exit_code=exit_code, error=error)
        return exit_code
    except Exception as e:
        head = f"{type(e).__name__}: {e}"
        tail = handler.tail()
        error = _finalize_run_error(tail, head=head)
        log_run_end(run_id, started_at, status="fail", exit_code=1, error=error)
        raise
    finally:
        root_logger.removeHandler(handler)
        handler.close()


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
                             timeout=_DEFAULT_TIMEOUT, session=None) -> list[int]:
    """Insert review Candidates as status='pending' rows into media_review.

    Returns the inserted rows' ids (PostgREST preserves array order; [] if empty).
    Raises SupabaseWriteError on non-2xx.
    """
    if not candidates:
        return []
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
    endpoint = f"{base_url}/rest/v1/media_review?select=id"
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=representation"}
    sess = session or requests.Session()
    try:
        resp = sess.post(endpoint, json=rows, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(f"media_review insert network error: {e}") from e
    if resp.status_code not in (200, 201, 204):
        raise SupabaseWriteError(f"media_review insert HTTP {resp.status_code}: {resp.text[:200]}")
    return [row["id"] for row in resp.json()]


def set_media_review_status(review_id, status, *, applied: bool = False,
                            url=None, service_key=None, timeout=_DEFAULT_TIMEOUT, session=None) -> None:
    """PATCH one media_review row's status (+ applied_at when applied=True).
    Raises SupabaseWriteError on non-2xx."""
    base_url, key = _resolve_credentials(url, service_key)
    payload: dict = {"status": status}
    if applied:
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


_DECISION_STATUS = {"approve": "approved", "reject": "rejected"}


def decide_media_review(review_id, decision, *, actor, url=None, service_key=None,
                        timeout=_DEFAULT_TIMEOUT, session=None) -> int:
    """Flip a PENDING media_review row to approved/rejected (the Phase 3 decision).

    Conditional on status='pending', so a repeat or an already-decided row is a
    no-op (returns 0). Records decided_by + decided_at. Returns rows updated (0/1).
    Raises ValueError on an unknown decision; SupabaseWriteError on non-2xx.
    """
    status = _DECISION_STATUS.get(decision)
    if status is None:
        raise ValueError(f"decision must be 'approve' or 'reject', got {decision!r}")
    base_url, key = _resolve_credentials(url, service_key)
    payload = {
        "status": status,
        "decided_by": actor,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    endpoint = f"{base_url}/rest/v1/media_review?id=eq.{int(review_id)}&status=eq.pending"
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=representation"}
    sess = session or requests.Session()
    try:
        resp = sess.patch(endpoint, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise SupabaseWriteError(f"media_review decide network error: {e}") from e
    if resp.status_code not in (200, 204):
        raise SupabaseWriteError(f"media_review decide HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return len(resp.json())
    except Exception:  # noqa: BLE001 — return=minimal or empty body → treat as 0
        return 0


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
        except Exception as e:  # noqa: BLE001
            logger.debug("upsert_metric_definitions_seed: could not parse response JSON: %s", e)
            return 0
    except SupabaseWriteError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("upsert_metric_definitions_seed failed: %s", e)
        raise


# ============================================================================
# MONTHLY metric system — metric_history_monthly / metric_definitions_monthly
# ----------------------------------------------------------------------------
# A SEPARATE namespace from metric_history/metric_definitions above -- see
# AGENTS.md landmine 20 ("Two parallel metric systems -- don't mix
# namespaces"). Monthly ids are always suffixed `_monthly`. Today the
# namespace is normally populated by the one-off seed/backfill scripts under
# scripts/ (seed_macro_monthly.py, backfill_call_money_monthly.py), each of
# which posts via its own small private `_upsert` with
# `Prefer: resolution=merge-duplicates,return=minimal` on BOTH tables --
# NOTE this differs from upsert_metric_definitions_seed above (daily
# namespace), which is ignore-duplicates/first-insert-wins. These two
# functions mirror that monthly-namespace convention exactly for callers
# OUTSIDE scripts/ -- e.g. aggregate_latest.py writing the bb_forex
# gross/BPM6 reserves split (D5, reserves-memo-2026-08-05) -- so a live
# writer can land rows in the monthly namespace without re-implementing the
# POST plumbing a third time.
# ============================================================================

_MONTHLY_HISTORY_TABLE = "metric_history_monthly"
_MONTHLY_DEFINITIONS_TABLE = "metric_definitions_monthly"


def _upsert_monthly_table(
    table: str,
    rows: list[dict],
    on_conflict: str,
    *,
    url: str | None = None,
    service_key: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> int:
    """Shared POST helper for the two monthly-namespace tables below.

    Normalises any `date` values to ISO strings (rows may be built with
    `date` objects for `as_of`/`source_as_of`, matching the daily writer's
    ergonomics) then upserts with merge-duplicates -- a re-run updates the
    existing (conflict-key) row rather than erroring.
    """
    if not rows:
        return 0
    base_url, key = _resolve_credentials(url, service_key)
    normalised: list[dict] = []
    for row in rows:
        r = dict(row)
        for field in ("as_of", "source_as_of"):
            if isinstance(r.get(field), date):
                r[field] = r[field].isoformat()
        normalised.append(r)

    endpoint = f"{base_url}/rest/v1/{table}?on_conflict={on_conflict}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    sess = session or requests.Session()
    upserted = 0
    for start in range(0, len(normalised), _BATCH_SIZE):
        batch = normalised[start : start + _BATCH_SIZE]
        try:
            resp = sess.post(endpoint, json=batch, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise SupabaseWriteError(f"network error during {table} upsert: {e}") from e
        if resp.status_code not in (200, 201, 204):
            raise SupabaseWriteError(
                f"{table} upsert returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        upserted += len(batch)
    return upserted


def upsert_metric_history_monthly(
    rows: list[dict],
    *,
    url: str | None = None,
    service_key: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> int:
    """Upsert rows into metric_history_monthly on (metric_id, as_of).

    Each row: {metric_id, as_of (date or ISO str), value, source,
    source_as_of? (date or ISO str)}. Mirrors
    scripts/seed_macro_monthly.py's private `_upsert` exactly (same
    on_conflict key, same merge-duplicates Prefer header) -- no `ingested_at`
    is posted, matching that convention (unlike the daily
    `upsert_metric_history`, which posts it explicitly per landmine E1.1;
    the monthly namespace has never needed the write-liveness distinction
    that fix addressed).

    Raises:
        SupabaseWriteError: on missing creds, network failure, or non-2xx.
    """
    return _upsert_monthly_table(
        _MONTHLY_HISTORY_TABLE, rows, "metric_id,as_of",
        url=url, service_key=service_key, timeout=timeout, session=session,
    )


def upsert_metric_definitions_monthly(
    definitions: list[dict],
    *,
    url: str | None = None,
    service_key: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> int:
    """Upsert metric_definitions_monthly rows on metric_id (merge-duplicates).

    Unlike upsert_metric_definitions_seed (daily namespace,
    ignore-duplicates/first-insert-wins so manual Studio edits survive), the
    monthly namespace's own seed scripts use merge-duplicates -- an edited
    definition (label, unit, notes) re-lands on the next write. Matches
    scripts/seed_macro_monthly.py._upsert exactly.

    Raises:
        SupabaseWriteError: on missing creds, network failure, or non-2xx.
    """
    return _upsert_monthly_table(
        _MONTHLY_DEFINITIONS_TABLE, definitions, "metric_id",
        url=url, service_key=service_key, timeout=timeout, session=session,
    )
