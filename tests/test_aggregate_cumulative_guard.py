"""Tests for the cumulative-monotonicity guard in aggregate_latest.py."""
from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture(autouse=True)
def skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


class TestFiscalYear:
    def test_july_starts_new_fy(self):
        from aggregate_latest import _fiscal_year
        assert _fiscal_year(date(2026, 7, 1)) == 2026
        assert _fiscal_year(date(2026, 6, 30)) == 2025
        assert _fiscal_year(date(2026, 1, 15)) == 2025


class TestCumulativeRegression:
    def test_same_fy_big_drop_is_regression(self):
        from aggregate_latest import _is_cumulative_regression
        # 287862 -> 33522 within FY2025 (both before next July)
        assert _is_cumulative_regression(33522.0, 287862.59, date(2026, 5, 31), date(2026, 5, 30)) is True

    def test_same_fy_small_dip_within_tolerance_is_ok(self):
        from aggregate_latest import _is_cumulative_regression
        # 2% downward revision — allowed
        assert _is_cumulative_regression(282000.0, 287862.59, date(2026, 5, 31), date(2026, 5, 30)) is False

    def test_rise_is_never_a_regression(self):
        from aggregate_latest import _is_cumulative_regression
        assert _is_cumulative_regression(300000.0, 287862.59, date(2026, 5, 31), date(2026, 5, 30)) is False

    def test_july_reset_across_fy_is_allowed(self):
        from aggregate_latest import _is_cumulative_regression
        # prior good from FY2025 (June), today early FY2026 (July) — legitimate reset
        assert _is_cumulative_regression(20000.0, 287862.59, date(2026, 7, 5), date(2026, 6, 28)) is False

    def test_non_numeric_today_is_not_regression(self):
        from aggregate_latest import _is_cumulative_regression
        assert _is_cumulative_regression(None, 287862.59, date(2026, 5, 31), date(2026, 5, 30)) is False


class TestPriorGoodSnapshot:
    def test_returns_most_recent_good_before_today(self, tmp_path, monkeypatch):
        import aggregate_latest
        d = tmp_path / "tax_revenue"
        d.mkdir()
        (d / "2026-05-30.json").write_text(
            '{"value": 287862.59, "scraped_at": "2026-05-30T05:00:00+00:00", "_provenance": "llm_extracted"}'
        )
        (d / "2026-05-31.json").write_text(
            '{"value": 33522.0, "scraped_at": "2026-05-31T05:00:00+00:00", "_provenance": "llm_extracted"}'
        )
        monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path)
        snap = aggregate_latest._prior_good_snapshot("tax_revenue", date(2026, 5, 31))
        assert snap is not None
        assert snap["value"] == 287862.59

    def test_returns_none_when_no_prior(self, tmp_path, monkeypatch):
        import aggregate_latest
        d = tmp_path / "tax_revenue"
        d.mkdir()
        (d / "2026-05-31.json").write_text(
            '{"value": 33522.0, "scraped_at": "2026-05-31T05:00:00+00:00", "_provenance": "llm_extracted"}'
        )
        monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path)
        assert aggregate_latest._prior_good_snapshot("tax_revenue", date(2026, 5, 31)) is None


class TestGuardInBuildBlocks:
    def _write(self, root, ind_id, dates_values):
        d = root / ind_id
        d.mkdir()
        for ds, val in dates_values:
            (d / f"{ds}.json").write_text(
                f'{{"value": {val}, "scraped_at": "{ds}T05:00:00+00:00", '
                f'"_provenance": "llm_extracted", "change_pct": null}}'
            )

    def test_cumulative_regression_uses_prior_good(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone

        import aggregate_latest
        self._write(tmp_path, "tax_revenue", [("2026-05-30", 287862.59), ("2026-05-31", 33522.0)])
        monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            aggregate_latest, "_load_v3_registry",
            lambda: [{"id": "tax_revenue", "domain": "government_finance",
                      "cadence": "monthly", "cumulative": True}],
        )
        data_additions, _, _, _ = aggregate_latest._build_v3_blocks(
            datetime(2026, 5, 31, 6, 0, tzinfo=timezone.utc)
        )
        # guard replaced the regressed 33522 with the prior-good 287862.59
        assert data_additions["tax_revenue"] == 287862.59

    def test_non_cumulative_drop_is_untouched(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone

        import aggregate_latest
        self._write(tmp_path, "some_level", [("2026-05-30", 100.0), ("2026-05-31", 10.0)])
        monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            aggregate_latest, "_load_v3_registry",
            lambda: [{"id": "some_level", "domain": "macro", "cadence": "daily"}],
        )
        data_additions, _, _, _ = aggregate_latest._build_v3_blocks(
            datetime(2026, 5, 31, 6, 0, tzinfo=timezone.utc)
        )
        assert data_additions["some_level"] == 10.0  # no guard for non-cumulative
