"""Scoped persistence accounting; no receipt may infer success from row counts.

A monthly scope (one appender) holds one or more LEGS. A leg is an independent
chart series family (the macro appender's cpi / remittance / imports / m2, or
a whole single-series appender such as yield or reserves). Every event names
its leg, the operation (read / write / readback) and a reason category, so a
dead source page, a refused guard and a month BB has not published yet can
never read alike. Failure dominates; confirmed row counts are never erased.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterator

# Skip categories: nothing was written, and that is not a persistence failure.
NO_NEW_OFFICIAL_PERIOD = "no new official period"
# A leg derived from our own daily table (CPI, M2) whose newest vintage is already recorded.
# It says nothing about BB: a daily scraper dead for months reads the same as publication lag.
NO_NEWER_DATABASE_VINTAGE = "no newer database vintage"
WITHHELD_BY_VALIDATION = "withheld by validation"
NOT_ATTEMPTED = "not attempted"
# A source whose newest listed month is older than one already recorded: lag cannot
# explain that (possible partial table change), so it is never "no new official period".
SOURCE_OLDER_THAN_RECORDS = "source older than recorded months"
# Daily NBR fiscal-year-to-date rows (R2 fix H5, owner decision m): the same value for a period
# metric_history already holds is not re-sent, and a figure whose source states no period is
# never dated at all (unknown stays unknown).
PERIOD_ALREADY_RECORDED = "period already recorded"
NO_SOURCE_PERIOD = "no source period"
# Failure categories.
DATABASE_READ_FAILED = "database read failed"
SOURCE_FETCH_FAILED = "source fetch failed"
SOURCE_PARSE_FAILED = "source parse failed"
DERIVATION_FAILED = "derivation failed"
DATABASE_WRITE_FAILED = "database write failed"
READBACK_MISMATCH = "readback mismatch"
# The exact (metric_id, as_of) row a write reported sending is not in the table at all.
READBACK_MISSING = "readback missing"
READBACK_UNAVAILABLE = "readback unavailable"
WRITE_UNCONFIRMED = "write unconfirmed"
UNHANDLED_EXCEPTION = "unhandled exception"


@dataclass
class _Leg:
    confirmed_rows: int = 0
    failures: list[dict] = field(default_factory=list)
    skips: list[dict] = field(default_factory=list)

    def has_events(self) -> bool:
        return bool(self.confirmed_rows or self.failures or self.skips)

    def result(self, attempted_at: str) -> dict:
        status = 'failed' if self.failures else 'ok' if self.confirmed_rows else 'skipped'
        return dict(status=status, attempted_at=attempted_at, reason=self._reason(),
                    confirmed_rows=self.confirmed_rows,
                    failures=[dict(f) for f in self.failures], skips=[dict(s) for s in self.skips])

    def _reason(self) -> str:
        parts = [f"{f['operation']} failed ({f['category']}): {f['detail']}" for f in self.failures]
        if self.confirmed_rows:
            parts.append(f"{self.confirmed_rows} row(s) written and confirmed by exact readback")
        parts.extend(f"{s['category']}: {s['detail']}" for s in self.skips)
        return '; '.join(parts) or 'no rows written (reason not itemised by this writer)'


@dataclass
class MonthlyReceipt:
    default_leg: str = 'monthly'
    leg_of: Mapping[str, str] = field(default_factory=dict)
    attempted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    legs: dict[str, _Leg] = field(default_factory=dict)
    _recorded: set[int] = field(default_factory=set)

    @property
    def confirmed_rows(self) -> int:
        return sum(leg.confirmed_rows for leg in self.legs.values())

    @property
    def failures(self) -> list[dict]:
        return [f for leg in self.legs.values() for f in leg.failures]

    def leg(self, name: str | None = None) -> _Leg:
        return self.legs.setdefault(name or self.default_leg, _Leg())

    def declare(self, *names: str) -> None:
        """Child legs that must always be reported, even when nothing happened to them."""
        for name in names:
            self.leg(name)

    def fail(self, reason: str) -> None:
        self.fail_leg(None, 'unknown', UNHANDLED_EXCEPTION, reason)

    def fail_leg(self, leg: str | None, operation: str, category: str, detail: str) -> None:
        self.leg(leg).failures.append(dict(operation=operation, category=category, detail=detail))

    def skip_leg(self, leg: str | None, category: str, detail: str,
                 newest_recorded: date | None = None, lag_window_days: int | None = None) -> None:
        """For publication lag: `newest_recorded` is the newest official month already stored and
        `lag_window_days` the accepted lag window for that series, so a reader can age the one
        against the other (R2 fix 4). Each is left out when not known."""
        skip = dict(category=category, detail=detail)
        if newest_recorded is not None:
            skip["newest_recorded"] = newest_recorded.isoformat()
        if lag_window_days is not None:
            skip["lag_window_days"] = lag_window_days
        self.leg(leg).skips.append(skip)

    def fail_unrecorded(self, leg: str | None, exc: BaseException) -> None:
        """An exception a leg-aware hook already charged is not charged twice."""
        if id(exc) not in self._recorded:
            self.fail_leg(leg, 'unknown', UNHANDLED_EXCEPTION, type(exc).__name__)

    def write_failed(self, rows: list[dict], exc: BaseException) -> None:
        self._recorded.add(id(exc))
        for leg, ids in self._ids_by_leg(rows).items():
            count = sum(1 for r in rows if self._leg_name(r['metric_id']) == leg)
            self.fail_leg(leg, 'write', DATABASE_WRITE_FAILED,
                          f"{count} row(s) for {', '.join(ids)} ({type(exc).__name__})")

    def confirm(self, rows: list[dict]) -> None:
        """Read back the exact (metric_id, as_of) keys written -- no recency window."""
        from utils.supabase_reader import get_metric_history_monthly_at

        for mid in sorted({r['metric_id'] for r in rows}):
            mine = [r for r in rows if r['metric_id'] == mid]
            leg = self._leg_name(mid)
            try:
                stored = get_metric_history_monthly_at(mid, [str(r['as_of'])[:10] for r in mine])
            except Exception as exc:  # noqa: BLE001 -- an unreadable table is unconfirmed, not ok
                self.fail_leg(leg, 'readback', READBACK_UNAVAILABLE, f"{mid} ({type(exc).__name__})")
                continue
            for row in mine:
                self._confirm_row(leg, row, stored)

    def _confirm_row(self, leg: str, row: dict, stored: list[dict]) -> None:
        found = next((r for r in stored if str(r.get('as_of'))[:10] == str(row['as_of'])[:10]), None)
        if found is None or any(found.get(k) != row.get(k)
                                for k in ('value', 'source', 'source_as_of') if k in row):
            self.fail_leg(leg, 'readback', READBACK_MISMATCH, f"{row['metric_id']} {row['as_of']}")
        else:
            self.leg(leg).confirmed_rows += 1

    def _leg_name(self, metric_id: str) -> str:
        return self.leg_of.get(metric_id, self.default_leg)

    def _ids_by_leg(self, rows: list[dict]) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for mid in sorted({r['metric_id'] for r in rows}):
            grouped.setdefault(self._leg_name(mid), []).append(mid)
        return grouped

    def result(self) -> dict:
        children = {name: leg for name, leg in self.legs.items()
                    if name != self.default_leg or leg.has_events()}
        if set(children) <= {self.default_leg}:
            return self.leg().result(self.attempted_at)
        return combine_monthly({name: leg.result(self.attempted_at)
                                for name, leg in children.items()})


_ACTIVE: ContextVar[MonthlyReceipt | None] = ContextVar('monthly_write_receipt', default=None)


@contextmanager
def monthly_attempt(leg: str = 'monthly',
                    leg_of: Mapping[str, str] | None = None) -> Iterator[MonthlyReceipt]:
    receipt = MonthlyReceipt(default_leg=leg, leg_of=dict(leg_of or {}))
    token = _ACTIVE.set(receipt)
    try:
        yield receipt
    finally:
        _ACTIVE.reset(token)


def declare_legs(*names: str) -> None:
    receipt = _ACTIVE.get()
    if receipt is not None:
        receipt.declare(*names)


def record_failure(leg: str, operation: str, category: str, detail: str) -> None:
    receipt = _ACTIVE.get()
    if receipt is not None:
        receipt.fail_leg(leg, operation, category, detail)


def record_skip(leg: str, category: str, detail: str, newest_recorded: date | None = None,
                lag_window_days: int | None = None) -> None:
    receipt = _ACTIVE.get()
    if receipt is not None:
        receipt.skip_leg(leg, category, detail, newest_recorded, lag_window_days)


def confirm_monthly(rows: list[dict]) -> None:
    receipt = _ACTIVE.get()
    if receipt is not None:
        receipt.confirm(rows)


def record_write_failure(rows: list[dict], exc: BaseException) -> None:
    receipt = _ACTIVE.get()
    if receipt is not None:
        receipt.write_failed(rows, exc)


def combine_monthly(parts: dict[str, dict]) -> dict:
    """Failures dominate without erasing independent successful legs or their row counts."""
    states = {part['status'] for part in parts.values()}
    return dict(status='failed' if 'failed' in states else 'ok' if 'ok' in states else 'skipped',
                attempted_at=max(part['attempted_at'] for part in parts.values()),
                reason='; '.join(f"{name}: {part['reason']}" for name, part in parts.items()),
                confirmed_rows=sum(part.get('confirmed_rows', 0) for part in parts.values()),
                legs=parts)
