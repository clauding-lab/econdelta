"""Unit tests for the DSE DS30 daily close scraper (scrapers/dse_dayend.py).

The daily scraper is a thin self-healing entry-point over the one-time backfill:
it computes a short look-back window and delegates to ``run_backfill`` for a REAL
(non-dry, full-30-ticker) write. These tests pin (1) the window math and (2) that
``main()`` delegates a real full-set write over that window — the two behaviours
that distinguish the daily job from the backfill CLI.

No network, no Supabase: ``run_backfill`` is mocked at the module boundary.
"""
from __future__ import annotations

from datetime import date

import scrapers.dse_dayend as dse_dayend


def test_compute_window_is_five_day_lookback_ending_today():
    """Default window self-heals the last 5 days, ending on the given day."""
    start, end = dse_dayend.compute_window(date(2026, 6, 2))
    assert end == date(2026, 6, 2)
    assert start == date(2026, 5, 28)  # 5 calendar days back
    assert (end - start).days == 5


def test_compute_window_respects_custom_lookback():
    start, end = dse_dayend.compute_window(date(2026, 6, 2), lookback_days=10)
    assert start == date(2026, 5, 23)
    assert end == date(2026, 6, 2)


def test_main_delegates_real_full_set_write_over_self_healing_window(monkeypatch):
    """main() must call run_backfill with dry_run=False, sample_only=False (all
    30 tickers), and a 5-day look-back ending today — NOT a dry-run 3-scrip sample."""
    captured: dict = {}

    def fake_run_backfill(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(dse_dayend, "run_backfill", fake_run_backfill)

    rc = dse_dayend.main()

    assert rc == 0
    assert captured["dry_run"] is False           # real write, not a dry-run
    assert captured["sample_only"] is False        # all 30, not a 3-scrip sample
    assert captured.get("codes_override") is None  # => run_backfill fetches DS30 live
    assert captured["end"] == date.today()         # window ends today
    assert (captured["end"] - captured["start"]).days == 5  # self-healing 5-day look-back


def test_main_propagates_run_backfill_failure_exit_code(monkeypatch):
    """A non-zero run_backfill exit (e.g. all fetches failed) must propagate so
    wrap_run records the run as failed, not ok."""
    monkeypatch.setattr(dse_dayend, "run_backfill", lambda **kw: 1)
    assert dse_dayend.main() == 1
