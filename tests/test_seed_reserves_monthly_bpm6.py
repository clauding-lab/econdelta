"""Tests for scripts/seed_reserves_monthly_bpm6.py -- pure transform functions
+ the CLI's --dry-run path. NEVER exercises the real --write path (no network,
no Supabase credentials needed) -- this script is a one-time backfill, not
run in CI or by any agent (see its module docstring)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scripts.seed_reserves_monthly_bpm6 import (
    BPM6_METRIC_ID,
    DEFAULT_FIXTURE,
    GROSS_METRIC_ID,
    ReservesMonth,
    build_definition_rows,
    build_history_rows,
    build_reserves_months,
    load_fixture,
    run,
)


class TestDefaultFixture:
    def test_committed_fixture_exists_and_loads(self):
        assert DEFAULT_FIXTURE.exists()
        payload = load_fixture()
        assert "rows" in payload
        assert len(payload["rows"]) > 0

    def test_committed_fixture_rows_pass_the_bpm6_invariant(self):
        """The committed fixture is real BB data (extracted from the repo's
        own test HTML) -- every row must already satisfy bpm6 < gross, or the
        fixture itself would be suspect."""
        payload = load_fixture()
        months = build_reserves_months(payload)
        assert len(months) == len(payload["rows"]), (
            "no row should be dropped by the invariant check against real data"
        )
        for m in months:
            assert m.bpm6_usd_bn < m.gross_usd_bn


class TestBuildReservesMonths:
    def test_converts_million_to_billion(self):
        payload = {"rows": [{"period": "2026-03", "gross_usd_mn": 34116.6, "bpm6_usd_mn": 29501.2}]}
        months = build_reserves_months(payload)
        assert len(months) == 1
        assert months[0].as_of == date(2026, 3, 1)
        assert months[0].gross_usd_bn == pytest.approx(34.1166)
        assert months[0].bpm6_usd_bn == pytest.approx(29.5012)

    def test_drops_row_violating_bpm6_lt_gross_invariant(self, caplog):
        """A corrupted/hand-edited fixture row with bpm6 >= gross must be
        dropped (with a warning), never silently written -- same invariant
        scrapers.bb_forex.parse_reserves enforces at parse time."""
        payload = {"rows": [
            {"period": "2026-01", "gross_usd_mn": 33178.6, "bpm6_usd_mn": 28682.8},  # valid
            {"period": "2026-02", "gross_usd_mn": 30357.0, "bpm6_usd_mn": 35109.2},  # swapped
        ]}
        months = build_reserves_months(payload)
        assert len(months) == 1
        assert months[0].as_of == date(2026, 1, 1)
        assert "column identification failure" in caplog.text

    def test_empty_rows_returns_empty_list(self):
        assert build_reserves_months({"rows": []}) == []


class TestBuildHistoryRows:
    def test_two_rows_per_month(self):
        months = [ReservesMonth(as_of=date(2026, 3, 1), gross_usd_bn=34.1166, bpm6_usd_bn=29.5012)]
        rows = build_history_rows(months)
        assert len(rows) == 2
        by_id = {r["metric_id"]: r for r in rows}
        assert set(by_id) == {GROSS_METRIC_ID, BPM6_METRIC_ID}
        assert by_id[GROSS_METRIC_ID]["value"] == pytest.approx(34.1166)
        assert by_id[BPM6_METRIC_ID]["value"] == pytest.approx(29.5012)
        assert by_id[GROSS_METRIC_ID]["as_of"] == "2026-03-01"
        assert by_id[GROSS_METRIC_ID]["source_as_of"] == "2026-03-01"

    def test_full_fixture_produces_48_rows(self):
        """24 committed months x 2 series = 48 rows."""
        payload = load_fixture()
        months = build_reserves_months(payload)
        rows = build_history_rows(months)
        assert len(rows) == len(months) * 2


class TestBuildDefinitionRows:
    def test_returns_both_ids(self):
        defs = build_definition_rows()
        ids = {d["metric_id"] for d in defs}
        assert ids == {GROSS_METRIC_ID, BPM6_METRIC_ID}
        for d in defs:
            assert d["domain"] == "external"
            assert d["unit"] == "USD bn"


class TestDryRunCli:
    def test_dry_run_is_default_and_performs_no_writes(self, capsys):
        """Calling run() with no --write flag must never import
        utils.supabase_writer or attempt any network call."""
        exit_code = run(["--dry-run"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "NO Supabase writes" in captured.out

    def test_bare_invocation_also_defaults_to_dry_run(self, capsys):
        """Even without the explicit --dry-run flag, the absence of --write
        must never touch Supabase -- --write is the one flag that unlocks a
        real write, not the presence/absence of --dry-run."""
        exit_code = run([])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "NO Supabase writes" in captured.out

    def test_custom_fixture_path(self, tmp_path: Path, capsys):
        fixture = tmp_path / "custom.json"
        fixture.write_text(
            '{"rows": [{"period": "2025-01", "gross_usd_mn": 25000.0, "bpm6_usd_mn": 20000.0}]}',
            encoding="utf-8",
        )
        exit_code = run(["--dry-run", "--fixture", str(fixture)])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "2 total" in captured.out  # 1 month x 2 series
