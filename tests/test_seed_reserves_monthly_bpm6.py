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

    def test_committed_fixture_covers_27_months_through_june_2026(self):
        """2026-08-05 review M6: the seed was extended with a fresh live BB
        capture so the chart doesn't have a permanent Apr-Jul 2026 hole
        while only the current month gets written going forward."""
        payload = load_fixture()
        periods = sorted(r["period"] for r in payload["rows"])
        assert len(periods) == 27
        assert periods[0] == "2024-04"
        assert periods[-1] == "2026-06"

    def test_committed_fixture_rows_pass_both_invariants(self):
        """The committed fixture is real BB data (extracted from the repo's
        own test HTML + a fresh live capture) -- every row must already
        satisfy both the direction and ratio-band checks, or the fixture
        itself would be suspect."""
        payload = load_fixture()
        months = build_reserves_months(payload)
        assert len(months) == len(payload["rows"]), (
            "no row should be dropped by either invariant check against real data"
        )
        for m in months:
            assert m.bpm6_usd_bn < m.gross_usd_bn
            ratio = m.bpm6_usd_bn / m.gross_usd_bn
            assert 0.70 <= ratio <= 0.95, f"{m.as_of}: ratio {ratio:.4f} outside band"

    def test_committed_fixture_as_of_values_are_all_month_end(self):
        """2026-08-05 review H3: as_of must be the LAST day of each month,
        matching aggregate_latest._write_reserves_monthly_split's
        _month_end() convention -- otherwise seeded history and live rows
        for the same month would sit ~30 days apart in the same series."""
        import calendar

        payload = load_fixture()
        months = build_reserves_months(payload)
        for m in months:
            last_day = calendar.monthrange(m.as_of.year, m.as_of.month)[1]
            assert m.as_of.day == last_day, f"{m.as_of} is not month-end"

    def test_june_2026_matches_reserves_memo_citation(self):
        """Cross-check against the D5 reserves-memo's independently-cited BB
        figure (37,578.0m gross for June 2026) -- confirms the live capture
        used to extend this fixture is genuine, not fabricated."""
        payload = load_fixture()
        june_2026 = next(r for r in payload["rows"] if r["period"] == "2026-06")
        assert june_2026["gross_usd_mn"] == pytest.approx(37578.0)


class TestBuildReservesMonths:
    def test_converts_million_to_billion(self):
        payload = {"rows": [{"period": "2026-03", "gross_usd_mn": 34116.6, "bpm6_usd_mn": 29501.2}]}
        months = build_reserves_months(payload)
        assert len(months) == 1
        assert months[0].as_of == date(2026, 3, 31)  # month-END (H3)
        assert months[0].gross_usd_bn == pytest.approx(34.1166)
        assert months[0].bpm6_usd_bn == pytest.approx(29.5012)

    def test_as_of_is_month_end_not_month_start(self):
        """Direct coverage of H3 -- February in a leap year exercises the
        real monthrange() lookup, not a hardcoded day-31."""
        payload = {"rows": [{"period": "2024-02", "gross_usd_mn": 25000.0, "bpm6_usd_mn": 21000.0}]}
        months = build_reserves_months(payload)
        assert months[0].as_of == date(2024, 2, 29)  # 2024 is a leap year

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
        assert months[0].as_of == date(2026, 1, 31)
        assert "column identification failure" in caplog.text

    def test_drops_row_violating_ratio_band(self, caplog):
        """2026-08-05 review H2: a same-direction magnitude corruption
        (gross ~122x too large) still satisfies bpm6 < gross, so only the
        ratio-band check catches it."""
        payload = {"rows": [
            {"period": "2026-01", "gross_usd_mn": 33178.6, "bpm6_usd_mn": 28682.8},  # valid
            {"period": "2026-02", "gross_usd_mn": 3611660.0, "bpm6_usd_mn": 29501.2},  # corrupted
        ]}
        months = build_reserves_months(payload)
        assert len(months) == 1
        assert months[0].as_of == date(2026, 1, 31)
        assert "ratio" in caplog.text
        assert "outside" in caplog.text

    def test_empty_rows_returns_empty_list(self):
        assert build_reserves_months({"rows": []}) == []


class TestBuildHistoryRows:
    def test_two_rows_per_month(self):
        months = [ReservesMonth(as_of=date(2026, 3, 31), gross_usd_bn=34.1166, bpm6_usd_bn=29.5012)]
        rows = build_history_rows(months)
        assert len(rows) == 2
        by_id = {r["metric_id"]: r for r in rows}
        assert set(by_id) == {GROSS_METRIC_ID, BPM6_METRIC_ID}
        assert by_id[GROSS_METRIC_ID]["value"] == pytest.approx(34.1166)
        assert by_id[BPM6_METRIC_ID]["value"] == pytest.approx(29.5012)
        assert by_id[GROSS_METRIC_ID]["as_of"] == "2026-03-31"
        assert by_id[GROSS_METRIC_ID]["source_as_of"] == "2026-03-31"

    def test_full_fixture_produces_54_rows(self):
        """27 committed months x 2 series = 54 rows (2026-08-05 review M6
        extended the fixture from 24 to 27 months)."""
        payload = load_fixture()
        months = build_reserves_months(payload)
        rows = build_history_rows(months)
        assert len(rows) == len(months) * 2
        assert len(rows) == 54


class TestBuildDefinitionRows:
    def test_returns_both_ids(self):
        defs = build_definition_rows()
        ids = {d["metric_id"] for d in defs}
        assert ids == {GROSS_METRIC_ID, BPM6_METRIC_ID}
        for d in defs:
            assert d["domain"] == "external"
            assert d["unit"] == "USD bn"

    def test_grace_days_is_45(self):
        """2026-08-05 review M5: v_metric_freshness COALESCEs grace_days
        from metric_definitions_monthly -- a missing value makes is_fresh
        permanently unknown (NULL), not merely wrong."""
        for d in build_definition_rows():
            assert d["grace_days"] == 45

    def test_notes_match_seed_macro_monthly_key_map_byte_identical(self):
        """2026-08-05 review L1: display_name/unit/domain/notes must be
        byte-identical to aggregate_latest._reserves_monthly_definitions()
        and scripts/seed_macro_monthly.py's KEY_MAP entries for these same
        ids, so last-writer-wins on merge-duplicates upsert is invisible to
        anyone reading metric_definitions_monthly."""
        by_id = {d["metric_id"]: d for d in build_definition_rows()}
        assert by_id[GROSS_METRIC_ID]["notes"] == ""
        assert by_id[BPM6_METRIC_ID]["notes"] == (
            "Sparse — BB began reporting BPM6 ~2021; nulls for earlier months."
        )


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
