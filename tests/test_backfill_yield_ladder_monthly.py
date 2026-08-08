"""Tests for scripts/backfill_yield_ladder_monthly.py -- pure transform
functions + the CLI's --dry-run path, PLUS a real-subprocess regression test
for the Phase 1 PYTHONPATH lesson (2026-08-08 box incident). NEVER exercises
a real network/Supabase write (no real credentials supplied anywhere) --
this is a one-time backfill, not run in CI or by any agent (see its module
docstring)."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.backfill_yield_ladder_monthly import (
    ALL_BACKFILL_ROWS,
    DEFINITION_UPDATES,
    EXPECTED_PAIRS,
    REPO_ROOT,
    TBILL_91D_ROWS,
    TBILL_182D_ROWS,
    TBILL_364D_ROWS,
    YIELD_2Y_ROWS,
    YIELD_5Y_ROWS,
    YIELD_10Y_ROWS,
    YIELD_15Y_ROWS,
    YIELD_20Y_ROWS,
    BackfillRow,
    build_definition_rows,
    build_history_rows,
    run,
)

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "backfill_yield_ladder_monthly.py"

_ALL_8_TENOR_MONTHLY_IDS = {
    "tbill_91d_yield_monthly", "tbill_182d_yield_monthly", "tbill_364d_yield_monthly",
    "yield_2y_monthly", "yield_5y_monthly", "yield_10y_monthly",
    "yield_15y_monthly", "yield_20y_monthly",
}


class TestControllerComputedValues:
    """Pin the exact 24 controller-computed values (2026-08-08, Phase 2) --
    these must never drift without a fresh sign-off."""

    @pytest.mark.parametrize(
        "rows,expected",
        [
            (TBILL_91D_ROWS, {date(2026, 5, 1): 10.15, date(2026, 6, 1): 9.4399, date(2026, 7, 1): 9.7949}),
            (TBILL_182D_ROWS, {date(2026, 5, 1): 10.4085, date(2026, 6, 1): 9.7098, date(2026, 7, 1): 9.9901}),
            (TBILL_364D_ROWS, {date(2026, 5, 1): 10.5, date(2026, 6, 1): 9.74, date(2026, 7, 1): 10.09}),
            (YIELD_2Y_ROWS, {date(2026, 5, 1): 10.728, date(2026, 6, 1): 10.43, date(2026, 7, 1): 9.7085}),
            (YIELD_5Y_ROWS, {date(2026, 5, 1): 10.78, date(2026, 6, 1): 10.3502, date(2026, 7, 1): 9.7894}),
            (YIELD_10Y_ROWS, {date(2026, 5, 1): 10.9099, date(2026, 6, 1): 10.24, date(2026, 7, 1): 10.24}),
            (YIELD_15Y_ROWS, {date(2026, 5, 1): 11.0198, date(2026, 6, 1): 10.304, date(2026, 7, 1): 10.3425}),
            (YIELD_20Y_ROWS, {date(2026, 5, 1): 11.0875, date(2026, 6, 1): 10.34, date(2026, 7, 1): 10.4}),
        ],
    )
    def test_tenor_values(self, rows, expected):
        by_date = {r.as_of: r.value for r in rows}
        for as_of, value in expected.items():
            assert by_date[as_of] == pytest.approx(value)

    def test_all_rows_use_bb_auction_source(self):
        assert all(r.source == "bb_auction" for r in ALL_BACKFILL_ROWS)


class TestBuildHistoryRows:
    def test_transform_is_pure(self):
        assert build_history_rows() == build_history_rows()

    def test_builds_exactly_24_rows(self):
        assert len(build_history_rows()) == 24

    def test_covers_exactly_8_tenors_x_3_months(self):
        rows = build_history_rows()
        pairs = {(r["metric_id"], r["as_of"]) for r in rows}
        assert pairs == {(mid, as_of.isoformat()) for mid, as_of in EXPECTED_PAIRS}
        assert {r["metric_id"] for r in rows} == _ALL_8_TENOR_MONTHLY_IDS

    def test_as_of_uses_day_1_of_the_data_month_convention(self):
        """AGENTS.md landmine 51: every row must use day-1-of-data-month, or
        it forks a shadow series against the existing seeded rows."""
        rows = build_history_rows()
        for r in rows:
            assert r["as_of"].endswith("-01"), r["as_of"]
            assert r["source_as_of"] == r["as_of"]

    def test_scope_drift_raises_assertion_error(self):
        """A 25th row (or a row outside the controller-approved set) must
        be REFUSED, not silently written."""
        rogue = BackfillRow("tbill_91d_yield_monthly", date(2026, 8, 1), 9999.0, "rogue")
        with pytest.raises(AssertionError, match="scope drift"):
            build_history_rows(ALL_BACKFILL_ROWS + (rogue,))

    def test_covers_exactly_3_months_may_june_july(self):
        rows = build_history_rows()
        assert {r["as_of"] for r in rows} == {"2026-05-01", "2026-06-01", "2026-07-01"}


class TestBuildDefinitionRows:
    def test_covers_the_same_8_ids(self):
        rows = build_definition_rows()
        assert {r["metric_id"] for r in rows} == _ALL_8_TENOR_MONTHLY_IDS

    def test_rows_are_full_not_partial(self):
        """Phase 1's H1 lesson applied proactively: migration 0007 declares
        display_name/unit/domain NOT NULL with no DEFAULT -- a bulk
        PostgREST upsert is one INSERT ... ON CONFLICT DO UPDATE statement,
        and Postgres validates the INSERT's VALUES list against NOT NULL
        BEFORE the ON CONFLICT decision. A partial row would 23502 the
        WHOLE bulk upsert -- every row must carry every NOT NULL column."""
        required = {"metric_id", "display_name", "unit", "source_url",
                    "source_attribution", "domain", "description", "notes"}
        for r in build_definition_rows():
            assert required <= set(r.keys())
            assert r["display_name"]
            assert r["unit"] == "%"
            assert r["domain"] == "prices_policy"

    def test_display_names_match_seed_macro_monthly_key_map(self):
        """Byte-identical to scripts/seed_macro_monthly.py's KEY_MAP for
        these 8 ids (tb91/tb182/tbill364/tr2y/tr5y/tr10y/tr15y/tr20y)."""
        by_id = {r["metric_id"]: r["display_name"] for r in build_definition_rows()}
        assert by_id["tbill_91d_yield_monthly"] == "91-day T-bill yield"
        assert by_id["tbill_182d_yield_monthly"] == "182-day T-bill yield"
        assert by_id["tbill_364d_yield_monthly"] == "364-day T-bill yield"
        assert by_id["yield_2y_monthly"] == "2Y bond yield"
        assert by_id["yield_5y_monthly"] == "5Y bond yield"
        assert by_id["yield_10y_monthly"] == "10Y bond yield"
        assert by_id["yield_15y_monthly"] == "15Y bond yield"
        assert by_id["yield_20y_monthly"] == "20Y bond yield"

    def test_source_url_points_at_the_real_auction_results_page(self):
        for r in build_definition_rows():
            assert r["source_url"] == "https://www.bb.org.bd/en/index.php/monetaryactivity/treasury"

    def test_returns_a_fresh_copy_not_the_module_constant(self):
        rows = build_definition_rows()
        rows[0]["source_url"] = "mutated"
        assert DEFINITION_UPDATES[0]["source_url"] != "mutated"


class TestDryRunCLI:
    def test_dry_run_is_the_default_and_performs_zero_http(self, capsys):
        with patch("requests.Session") as mock_session_cls:
            exit_code = run([])
            mock_session_cls.assert_not_called()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "tbill_91d_yield_monthly" in captured.out

    def test_explicit_dry_run_flag_performs_zero_http(self):
        with patch("requests.Session") as mock_session_cls:
            exit_code = run(["--dry-run"])
            mock_session_cls.assert_not_called()
        assert exit_code == 0

    def test_write_path_writes_history_before_definitions(self):
        from unittest.mock import MagicMock

        manager = MagicMock()
        with patch(
            "utils.supabase_writer.upsert_metric_history_monthly", return_value=24
        ) as mock_hist, patch(
            "utils.supabase_writer.upsert_metric_definitions_monthly", return_value=8
        ) as mock_defs:
            manager.attach_mock(mock_hist, "history")
            manager.attach_mock(mock_defs, "definitions")
            exit_code = run(["--write"])
        assert exit_code == 0
        call_order = [c[0] for c in manager.mock_calls]
        assert call_order.index("history") < call_order.index("definitions")
        written_rows = mock_hist.call_args[0][0]
        assert len(written_rows) == 24

    def test_write_path_returns_1_on_supabase_write_error(self):
        from utils.supabase_writer import SupabaseWriteError

        with patch(
            "utils.supabase_writer.upsert_metric_history_monthly",
            side_effect=SupabaseWriteError("boom"),
        ):
            exit_code = run(["--write"])
        assert exit_code == 1


class TestWritePathPythonPathRegression:
    """Regression test for the Phase 1 PYTHONPATH lesson (2026-08-08 box
    incident): scripts/backfill_monthly_chart_series.py's --write path
    failed with ModuleNotFoundError when invoked as a plain file path from
    repo root WITHOUT PYTHONPATH=. set -- --dry-run never caught it because
    the `from utils.supabase_writer import ...` line is inside the --write
    branch only.

    A plain in-process call to run(["--write"]) inside THIS pytest process
    would NOT have caught the original bug: pytest's own test collection
    already puts the repo root on sys.path (via tests/test_aggregator.py's
    own bootstrap, which every test file transitively benefits from once
    collected), so the ModuleNotFoundError would be masked here. This test
    instead spawns the script as a REAL subprocess with PYTHONPATH
    explicitly unset -- the exact failure condition -- to prove the
    script's OWN sys.path bootstrap (mirroring scripts/build_catalog.py's
    pattern) fixes it independent of the caller's environment."""

    def test_write_flag_does_not_raise_modulenotfounderror_without_pythonpath(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("SUPABASE_URL", None)
        env.pop("SUPABASE_SERVICE_ROLE_KEY", None)
        env.pop("SUPABASE_SERVICE_KEY", None)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--write"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert "ModuleNotFoundError" not in result.stderr, (
            f"PYTHONPATH regression: {result.stderr}"
        )
        # Expected failure mode WITHOUT real credentials: a clean
        # SupabaseWriteError -> return 1. Proves the import succeeded and
        # execution reached the credential-resolution step.
        assert result.returncode == 1, result.stderr
        assert "SUPABASE_URL" in result.stderr or "SupabaseWriteError" in result.stderr

    def test_dry_run_flag_also_works_without_pythonpath(self):
        """Sanity companion: --dry-run never exercised the buggy import
        path even before the fix, so this should always have passed --
        included so a future refactor can't silently break the (already
        fine) dry-run path while "fixing" the write path."""
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--dry-run"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "ModuleNotFoundError" not in result.stderr
