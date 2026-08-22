"""Tests for scripts/backfill_cpi_july_2026.py -- pure transform functions
+ the CLI's --dry-run path. NEVER exercises the real --write path (no
network, no Supabase credentials needed) -- this is a one-time backfill,
not run in CI or by any agent (see its module docstring)."""
from __future__ import annotations

from datetime import date

import pytest

from scripts.backfill_cpi_july_2026 import (
    ALL_BACKFILL_ROWS,
    CPI_FOOD_DERIVED_SOURCE,
    CPI_SOURCE,
    EXPECTED_PAIRS,
    JULY_2026,
    BackfillRow,
    build_history_rows,
    run,
)


class TestControllerVerifiedValues:
    def test_exactly_three_rows_for_july_2026(self):
        assert len(ALL_BACKFILL_ROWS) == 3
        assert all(r.as_of == JULY_2026 for r in ALL_BACKFILL_ROWS)

    def test_values(self):
        by_id = {r.metric_id: r.value for r in ALL_BACKFILL_ROWS}
        assert by_id["cpi_12m_avg_monthly"] == pytest.approx(8.66)
        assert by_id["cpi_p2p_food_monthly"] == pytest.approx(7.16)
        assert by_id["cpi_p2p_nonfood_monthly"] == pytest.approx(9.28)

    def test_source_labels(self):
        """Opus review round 1, M6: food's source label is DISTINCT from
        the other two -- it's arithmetically derived, not read off any BB
        page, and must never be confused with a genuine page-sourced
        reading."""
        by_id = {r.metric_id: r.source for r in ALL_BACKFILL_ROWS}
        assert by_id["cpi_12m_avg_monthly"] == CPI_SOURCE
        assert by_id["cpi_p2p_nonfood_monthly"] == CPI_SOURCE
        assert by_id["cpi_p2p_food_monthly"] == CPI_FOOD_DERIVED_SOURCE
        assert CPI_FOOD_DERIVED_SOURCE != CPI_SOURCE


class TestBuildHistoryRows:
    def test_row_shape(self):
        rows = build_history_rows()
        assert rows[0] == {
            "metric_id": "cpi_12m_avg_monthly", "as_of": "2026-07-01", "value": 8.66,
            "source": "bb_inflation_page", "source_as_of": "2026-07-01",
        }

    def test_food_row_carries_the_derived_source_label(self):
        rows = build_history_rows()
        by_id = {r["metric_id"]: r for r in rows}
        assert by_id["cpi_p2p_food_monthly"]["source"] == "derived_implied_weight_bb_inflation"

    def test_matches_expected_pairs_exactly(self):
        rows = build_history_rows()
        built = {(r["metric_id"], date.fromisoformat(r["as_of"])) for r in rows}
        assert built == EXPECTED_PAIRS

    def test_scope_drift_raises(self):
        drifted = ALL_BACKFILL_ROWS + (BackfillRow("some_other_id", JULY_2026, 1.0, "x"),)
        with pytest.raises(AssertionError, match="scope drift"):
            build_history_rows(drifted)


class TestDryRunCli:
    def test_dry_run_is_default_and_prints_summary(self, capsys):
        exit_code = run([])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "cpi_12m_avg_monthly" in out
        assert "8.66" in out

    def test_dry_run_flag_explicit(self, capsys):
        exit_code = run(["--dry-run"])
        assert exit_code == 0
        assert "DRY RUN" in capsys.readouterr().out
