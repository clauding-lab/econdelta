"""Tests for scripts/backfill_monthly_chart_series.py -- pure transform
functions + the CLI's --dry-run path. NEVER exercises the real --write path
(no network, no Supabase credentials needed) -- this is a one-time backfill,
not run in CI or by any agent (see its module docstring)."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from scripts.backfill_monthly_chart_series import (
    ALL_BACKFILL_ROWS,
    CPI_12M_AVG_ROWS,
    CPI_P2P_FOOD_ROWS,
    CPI_P2P_NONFOOD_ROWS,
    DEFINITION_SOURCE_UPDATES,
    EXPECTED_PAIRS,
    EXPORTS_ROWS,
    REMITTANCE_ROWS,
    BackfillRow,
    build_definition_rows,
    build_history_rows,
    run,
)


class TestOwnerApprovedValues:
    """Pin the exact 15 owner-approved values (2026-08-08) -- these must
    never drift without a fresh sign-off."""

    def test_remittance_values(self):
        by_date = {r.as_of: r.value for r in REMITTANCE_ROWS}
        assert by_date[date(2026, 4, 1)] == pytest.approx(3127.30)
        assert by_date[date(2026, 5, 1)] == pytest.approx(3442.58)
        assert by_date[date(2026, 6, 1)] == pytest.approx(2816.96)

    def test_exports_values(self):
        by_date = {r.as_of: r.value for r in EXPORTS_ROWS}
        assert by_date[date(2026, 4, 1)] == pytest.approx(4009.93)
        assert by_date[date(2026, 5, 1)] == pytest.approx(4402.78)
        assert by_date[date(2026, 6, 1)] == pytest.approx(4202.69)

    def test_cpi_12m_avg_values(self):
        by_date = {r.as_of: r.value for r in CPI_12M_AVG_ROWS}
        assert by_date[date(2026, 4, 1)] == pytest.approx(8.59)
        assert by_date[date(2026, 5, 1)] == pytest.approx(8.63)
        assert by_date[date(2026, 6, 1)] == pytest.approx(8.68)

    def test_cpi_p2p_food_values(self):
        by_date = {r.as_of: r.value for r in CPI_P2P_FOOD_ROWS}
        assert by_date[date(2026, 4, 1)] == pytest.approx(8.39)
        assert by_date[date(2026, 5, 1)] == pytest.approx(9.06)
        assert by_date[date(2026, 6, 1)] == pytest.approx(8.60)

    def test_cpi_p2p_nonfood_values(self):
        by_date = {r.as_of: r.value for r in CPI_P2P_NONFOOD_ROWS}
        assert by_date[date(2026, 4, 1)] == pytest.approx(9.57)
        assert by_date[date(2026, 5, 1)] == pytest.approx(9.71)
        assert by_date[date(2026, 6, 1)] == pytest.approx(9.61)


class TestBuildHistoryRows:
    def test_transform_is_pure(self):
        """Calling twice with the same input produces byte-identical output --
        no hidden state, no I/O."""
        assert build_history_rows() == build_history_rows()

    def test_builds_exactly_15_rows(self):
        rows = build_history_rows()
        assert len(rows) == 15

    def test_covers_exactly_5_ids_x_3_months(self):
        rows = build_history_rows()
        pairs = {(r["metric_id"], r["as_of"]) for r in rows}
        assert pairs == {(mid, as_of.isoformat()) for mid, as_of in EXPECTED_PAIRS}
        metric_ids = {r["metric_id"] for r in rows}
        assert metric_ids == {
            "remittance_usd_mn_monthly",
            "exports_usd_mn_monthly",
            "cpi_12m_avg_monthly",
            "cpi_p2p_food_monthly",
            "cpi_p2p_nonfood_monthly",
        }

    def test_as_of_uses_day_1_of_data_month_convention(self):
        """AGENTS.md landmine 50: every row must use day-1-of-data-month, or
        it forks a shadow series against the existing seeded rows."""
        rows = build_history_rows()
        for r in rows:
            assert r["as_of"].endswith("-01"), r["as_of"]
            assert r["source_as_of"] == r["as_of"]

    def test_scope_drift_raises_assertion_error(self):
        """A 16th row (or a row outside the owner-approved set) must be
        REFUSED, not silently written -- this is the hard guard against
        scope creep the spec requires."""
        rogue = BackfillRow("remittance_usd_mn_monthly", date(2026, 7, 1), 9999.0, "rogue")
        with pytest.raises(AssertionError, match="scope drift"):
            build_history_rows(ALL_BACKFILL_ROWS + (rogue,))

    def test_each_row_has_matching_source_label(self):
        by_id = {r["metric_id"]: r["source"] for r in build_history_rows()}
        assert by_id["remittance_usd_mn_monthly"] == "bb_wageremitance"
        assert by_id["exports_usd_mn_monthly"] == "epb_bss"
        assert by_id["cpi_12m_avg_monthly"] == "bb_inflation_page"
        assert by_id["cpi_p2p_food_monthly"] == "bb_inflation_page"
        assert by_id["cpi_p2p_nonfood_monthly"] == "bb_inflation_page"


class TestBuildDefinitionRows:
    def test_covers_the_same_5_ids(self):
        rows = build_definition_rows()
        assert {r["metric_id"] for r in rows} == {
            "remittance_usd_mn_monthly",
            "exports_usd_mn_monthly",
            "cpi_12m_avg_monthly",
            "cpi_p2p_food_monthly",
            "cpi_p2p_nonfood_monthly",
        }

    def test_rows_are_partial_source_only(self):
        """Only metric_id/source_url/source_attribution -- no display_name/
        unit/domain -- so the merge-duplicates upsert leaves the existing
        labels (from scripts/seed_macro_monthly.py's KEY_MAP) untouched."""
        for r in build_definition_rows():
            assert set(r.keys()) == {"metric_id", "source_url", "source_attribution"}

    def test_no_dead_macro_observer_site_remains(self):
        for r in build_definition_rows():
            assert "thenazmussakib" not in r["source_url"]

    def test_returns_a_fresh_copy_not_the_module_constant(self):
        rows = build_definition_rows()
        rows[0]["source_url"] = "mutated"
        assert DEFINITION_SOURCE_UPDATES[0]["source_url"] != "mutated"


class TestDryRunCLI:
    def test_dry_run_is_the_default_and_performs_zero_http(self, capsys):
        with patch("requests.Session") as mock_session_cls:
            exit_code = run([])
            mock_session_cls.assert_not_called()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "remittance_usd_mn_monthly" in captured.out

    def test_explicit_dry_run_flag_performs_zero_http(self):
        with patch("requests.Session") as mock_session_cls:
            exit_code = run(["--dry-run"])
            mock_session_cls.assert_not_called()
        assert exit_code == 0

    def test_write_path_calls_the_monthly_upsert_helpers(self):
        """--write is exercised here ONLY with the upsert functions mocked --
        no real network/Supabase call goes out."""
        with patch(
            "utils.supabase_writer.upsert_metric_history_monthly", return_value=15
        ) as mock_hist, patch(
            "utils.supabase_writer.upsert_metric_definitions_monthly", return_value=5
        ) as mock_defs:
            exit_code = run(["--write"])
        assert exit_code == 0
        mock_hist.assert_called_once()
        mock_defs.assert_called_once()
        written_rows = mock_hist.call_args[0][0]
        assert len(written_rows) == 15

    def test_write_path_returns_1_on_supabase_write_error(self):
        from utils.supabase_writer import SupabaseWriteError

        with patch(
            "utils.supabase_writer.upsert_metric_definitions_monthly",
            side_effect=SupabaseWriteError("boom"),
        ):
            exit_code = run(["--write"])
        assert exit_code == 1
