"""Tests for aggregate_latest._write_macro_monthly_append and its helpers
(2026-08-08 frozen-charts incident, AGENTS.md landmine 50).

Covers: the CPI-trio pure transform (mapping, append-only skip, month-end
vintage check, the general==p2p wrong-column equality guard, range check),
the remittance HTML parser (real fixture + synthetic FY-boundary case),
row selection (range/skip-if-exists), and the top-level orchestrator wired
against mocked reader/writer/fetch so no real network or Supabase call goes
out.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aggregate_latest as agg  # noqa: E402
from fetchers.base import FetchError  # noqa: E402
from utils.supabase_reader import SupabaseReadError  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# _latest_value_as_of
# ---------------------------------------------------------------------------


class TestLatestValueAsOf:
    def test_empty_rows_returns_none(self):
        assert agg._latest_value_as_of([]) is None

    def test_parses_first_row(self):
        rows = [{"value": "8.68", "as_of": "2026-06-30"}, {"value": "8.63", "as_of": "2026-05-31"}]
        assert agg._latest_value_as_of(rows) == (8.68, date(2026, 6, 30))

    def test_malformed_row_returns_none(self):
        assert agg._latest_value_as_of([{"value": "not-a-number", "as_of": "2026-06-30"}]) is None


# ---------------------------------------------------------------------------
# _cpi_monthly_append_rows — the CPI trio pure transform
# ---------------------------------------------------------------------------


class TestCpiMonthlyAppendRows:
    def test_maps_each_daily_id_to_its_monthly_id(self):
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=(8.60, date(2026, 6, 30)),
            nonfood_row=(9.61, date(2026, 6, 30)),
            p2p_row=(9.16, date(2026, 6, 30)),
            existing_pairs=set(),
        )
        assert reasons == []
        by_id = {r["metric_id"]: r for r in rows}
        assert set(by_id) == {"cpi_12m_avg_monthly", "cpi_p2p_food_monthly", "cpi_p2p_nonfood_monthly"}
        assert by_id["cpi_12m_avg_monthly"]["value"] == pytest.approx(8.68)
        assert by_id["cpi_p2p_food_monthly"]["value"] == pytest.approx(8.60)
        assert by_id["cpi_p2p_nonfood_monthly"]["value"] == pytest.approx(9.61)

    def test_as_of_uses_day_1_of_the_data_month(self):
        rows, _ = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=None, nonfood_row=None, p2p_row=None,
            existing_pairs=set(),
        )
        assert rows[0]["as_of"] == "2026-06-01"
        assert rows[0]["source_as_of"] == "2026-06-30"  # true recovered vintage, not day-1

    def test_missing_daily_row_is_skipped_with_a_reason(self):
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=None, food_row=None, nonfood_row=None, p2p_row=None,
            existing_pairs=set(),
        )
        assert rows == []
        assert len(reasons) == 3
        assert all("no daily" in r for r in reasons)

    def test_non_month_end_as_of_is_skipped(self):
        """A daily row whose as_of isn't the last day of its month isn't a
        true monthly vintage -- e.g. a run-date-forged as_of (landmine 26/47)."""
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 15)),  # not month-end
            food_row=None, nonfood_row=None, p2p_row=None,
            existing_pairs=set(),
        )
        assert rows == []
        assert "not a month-end vintage" in reasons[0]

    @pytest.mark.parametrize("bad_value", [-1.0, 0.0, 30.0, 45.2])
    def test_range_check_rejects_outside_0_to_30(self, bad_value):
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=(bad_value, date(2026, 6, 30)),
            food_row=None, nonfood_row=None, p2p_row=None,
            existing_pairs=set(),
        )
        assert rows == []
        assert "outside" in reasons[0]

    def test_general_equals_p2p_wrong_column_guard_skips_general_only(self):
        """Regression test for the June-2026 incident (AGENTS.md landmine 49):
        general_inflation's extractor grabbed the Point-to-Point column
        instead of Twelve-month-average, so it exactly matched
        point_to_point_inflation for 2026-06-30. cpi_12m_avg_monthly must be
        skipped for that month -- but food/non-food (unaffected by this
        column-family confusion) must still write normally."""
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=(9.16, date(2026, 6, 30)),   # == p2p below: wrong column
            food_row=(8.60, date(2026, 6, 30)),
            nonfood_row=(9.61, date(2026, 6, 30)),
            p2p_row=(9.16, date(2026, 6, 30)),
            existing_pairs=set(),
        )
        by_id = {r["metric_id"]: r for r in rows}
        assert "cpi_12m_avg_monthly" not in by_id
        assert "cpi_p2p_food_monthly" in by_id
        assert "cpi_p2p_nonfood_monthly" in by_id
        assert any("wrong CPI column" in r for r in reasons)

    def test_general_differing_from_p2p_is_not_guarded(self):
        """A genuine (non-equal) general_inflation reading must NOT be
        blocked by the guard -- it only fires on exact equality."""
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=None, nonfood_row=None,
            p2p_row=(9.16, date(2026, 6, 30)),  # different value -> no guard
            existing_pairs=set(),
        )
        assert {r["metric_id"] for r in rows} == {"cpi_12m_avg_monthly"}
        assert not any("wrong CPI column" in r for r in reasons)

    def test_equality_guard_requires_matching_as_of(self):
        """Equal VALUES on DIFFERENT months is coincidence, not the wrong-
        column defect -- the guard must compare same-as_of only."""
        rows, _reasons = agg._cpi_monthly_append_rows(
            general_row=(9.16, date(2026, 6, 30)),
            food_row=None, nonfood_row=None,
            p2p_row=(9.16, date(2026, 5, 31)),  # same value, different month
            existing_pairs=set(),
        )
        assert {r["metric_id"] for r in rows} == {"cpi_12m_avg_monthly"}

    def test_append_only_skips_existing_pair(self):
        """The backfill (scripts/backfill_monthly_chart_series.py) already
        wrote cpi_12m_avg_monthly for 2026-06-01 with an official value --
        the appender must NEVER clobber it with a re-derived daily value."""
        rows, _reasons = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=None, nonfood_row=None, p2p_row=None,
            existing_pairs={("cpi_12m_avg_monthly", date(2026, 6, 1))},
        )
        assert rows == []

    def test_writes_all_available_trio_siblings_in_one_batch(self):
        rows, _ = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=(8.60, date(2026, 6, 30)),
            nonfood_row=(9.61, date(2026, 6, 30)),
            p2p_row=(9.16, date(2026, 6, 30)),
            existing_pairs=set(),
        )
        assert len(rows) == 3
        assert {r["as_of"] for r in rows} == {"2026-06-01"}  # aligned, same run

    def test_source_label_is_econdelta_daily_cpi(self):
        rows, _ = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=None, nonfood_row=None, p2p_row=None,
            existing_pairs=set(),
        )
        assert rows[0]["source"] == "econdelta_daily_cpi"


# ---------------------------------------------------------------------------
# parse_remittance_table — real fixture + synthetic FY-boundary case
# ---------------------------------------------------------------------------


class TestParseRemittanceTable:
    def _real_html(self) -> str:
        return (FIXTURES_DIR / "bb_wageremitance.html").read_text(encoding="utf-8")

    def test_parses_the_real_fixture_backfill_months(self):
        """Cross-check against scripts/backfill_monthly_chart_series.py's
        owner-approved values -- this real BB capture is where those numbers
        came from."""
        parsed = dict(agg.parse_remittance_table(self._real_html()))
        assert parsed[date(2026, 4, 1)] == pytest.approx(3127.30)
        assert parsed[date(2026, 5, 1)] == pytest.approx(3442.58)
        assert parsed[date(2026, 6, 1)] == pytest.approx(2816.96)

    def test_parses_across_fiscal_year_boundary_within_the_fixture(self):
        """July under the "2025-2026" header must map to calendar year 2025
        (the FIRST year of the pair), while January under the SAME header
        maps to 2026 (the SECOND year) -- exercised naturally by the real
        fixture's FY26 block."""
        parsed = dict(agg.parse_remittance_table(self._real_html()))
        assert parsed[date(2025, 7, 1)] == pytest.approx(2477.87)   # July 2025 (FY26 start)
        assert parsed[date(2026, 1, 1)] == pytest.approx(3171.63)   # January 2026 (FY26 second half)

    def test_parses_three_full_fiscal_years_from_the_real_fixture(self):
        parsed = agg.parse_remittance_table(self._real_html())
        assert len(parsed) == 30  # FY26 (12) + FY25 (12) + partial FY24 (6 in the fixture)

    def test_fy_boundary_july_is_first_month_of_the_new_fy(self):
        """Synthetic minimal table (BB has not yet published a "2026-2027"
        block as of this fixture's capture date) proving the header-parsing
        regex correctly resolves a NOT-YET-SEEN fiscal year string: July
        under "2026-2027" is July 2026 (FY27's first month), not July 2027."""
        html = """
        <table id="sortableTable"><tbody>
          <tr><td colspan="3">2026-2027</td></tr>
          <tr><td>July</td><td>2900.00</td><td>355.00</td></tr>
        </tbody></table>
        """
        parsed = dict(agg.parse_remittance_table(html))
        assert parsed[date(2026, 7, 1)] == pytest.approx(2900.00)

    def test_no_table_raises_value_error(self):
        with pytest.raises(ValueError, match="no remittance table"):
            agg.parse_remittance_table("<html><body>nothing here</body></html>")

    def test_no_tbody_raises_value_error(self):
        with pytest.raises(ValueError, match="no <tbody>"):
            agg.parse_remittance_table('<table id="sortableTable"></table>')

    def test_unparseable_value_cell_is_skipped_not_crashed(self):
        html = """
        <table id="sortableTable"><tbody>
          <tr><td colspan="3">2026-2027</td></tr>
          <tr><td>July</td><td>n/a</td><td>-</td></tr>
          <tr><td>August</td><td>2500.00</td><td>305.00</td></tr>
        </tbody></table>
        """
        parsed = dict(agg.parse_remittance_table(html))
        assert date(2026, 7, 1) not in parsed
        assert parsed[date(2026, 8, 1)] == pytest.approx(2500.00)


# ---------------------------------------------------------------------------
# _select_new_remittance_rows
# ---------------------------------------------------------------------------


class TestSelectNewRemittanceRows:
    def test_only_months_on_or_after_the_backfill_cutoff_are_selected(self):
        parsed = [(date(2026, 6, 1), 2816.96), (date(2026, 7, 1), 2950.0)]
        rows, _ = agg._select_new_remittance_rows(parsed, existing_as_of=set())
        assert {r["as_of"] for r in rows} == {"2026-07-01"}

    def test_skip_if_exists_append_only(self):
        parsed = [(date(2026, 7, 1), 2950.0), (date(2026, 8, 1), 3010.0)]
        rows, _ = agg._select_new_remittance_rows(
            parsed, existing_as_of={date(2026, 7, 1)},
        )
        assert {r["as_of"] for r in rows} == {"2026-08-01"}

    @pytest.mark.parametrize("bad_value", [499.99, 6000.01, -100.0])
    def test_value_range_rejects_outside_500_to_6000(self, bad_value):
        parsed = [(date(2026, 7, 1), bad_value)]
        rows, reasons = agg._select_new_remittance_rows(parsed, existing_as_of=set())
        assert rows == []
        assert "outside" in reasons[0]

    def test_boundary_values_are_accepted(self):
        parsed = [(date(2026, 7, 1), 500.0), (date(2026, 8, 1), 6000.0)]
        rows, reasons = agg._select_new_remittance_rows(parsed, existing_as_of=set())
        assert len(rows) == 2
        assert reasons == []

    def test_source_label_is_bb_wageremitance(self):
        rows, _ = agg._select_new_remittance_rows(
            [(date(2026, 7, 1), 2950.0)], existing_as_of=set(),
        )
        assert rows[0]["source"] == "bb_wageremitance"
        assert rows[0]["metric_id"] == "remittance_usd_mn_monthly"


# ---------------------------------------------------------------------------
# _write_macro_monthly_append — orchestrator, wired against mocks
# ---------------------------------------------------------------------------


class TestWriteMacroMonthlyAppend:
    def _cpi_daily_row(self, value: float, as_of: date) -> list[dict]:
        return [{"metric_id": "x", "value": value, "as_of": as_of.isoformat(),
                  "source": "econdelta", "ingested_at": f"{as_of.isoformat()}T00:00:00+00:00"}]

    def test_writes_cpi_trio_when_all_fresh_and_new(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def fake_get_metric_history(metric_id, *, days, **kwargs):
            values = {
                "general_inflation": 8.68, "food_inflation": 8.60,
                "non_food_inflation": 9.61, "point_to_point_inflation": 9.16,
            }
            return self._cpi_daily_row(values[metric_id], date(2026, 6, 30))

        monkeypatch.setattr(reader, "get_metric_history", fake_get_metric_history)
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(
            agg, "_fetch_remittance_html", lambda: pytest.fail("remittance path not under test")
        )

        captured: list[dict] = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        # remittance sub-path fails gracefully (fetch stubbed to fail) so
        # only CPI rows reach the final upsert.
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("x")))
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_macro_monthly_append()
        assert n == 3
        ids = {r["metric_id"] for r in captured}
        assert ids == {"cpi_12m_avg_monthly", "cpi_p2p_food_monthly", "cpi_p2p_nonfood_monthly"}

    def test_cpi_read_failure_notifies_and_does_not_crash(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def raise_read(*a, **k):
            raise SupabaseReadError("boom")

        monkeypatch.setattr(reader, "get_metric_history", raise_read)
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("x")))
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: pytest.fail("no rows expected"))

        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

        n = agg._write_macro_monthly_append()
        assert n == 0
        assert any("CPI read failed" in title for _level, title in notify_calls)

    def test_remittance_fetch_failure_notifies_and_cpi_still_proceeds(self, monkeypatch):
        """(a) and (b) are independent -- a remittance-page failure must not
        block the CPI trio from writing."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def fake_get_metric_history(metric_id, *, days, **kwargs):
            return self._cpi_daily_row(8.68, date(2026, 6, 30))

        monkeypatch.setattr(reader, "get_metric_history", fake_get_metric_history)
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("unreachable")))

        captured: list[dict] = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

        n = agg._write_macro_monthly_append()
        assert n >= 1  # cpi_12m_avg_monthly (food/nonfood share the same stub value, still fine)
        assert any("remittance fetch/parse failed" in title for _level, title in notify_calls)
        assert all(r["metric_id"] != "remittance_usd_mn_monthly" for r in captured)

    def test_remittance_writes_when_fetch_and_parse_succeed(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        monkeypatch.setattr(reader, "get_metric_history", lambda *a, **k: [])
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])

        html = """
        <table id="sortableTable"><tbody>
          <tr><td colspan="3">2026-2027</td></tr>
          <tr><td>July</td><td>2950.00</td><td>360.00</td></tr>
        </tbody></table>
        """
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: html)

        captured: list[dict] = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_macro_monthly_append()
        assert n == 1
        assert captured[0]["metric_id"] == "remittance_usd_mn_monthly"
        assert captured[0]["as_of"] == "2026-07-01"

    def test_nothing_to_write_returns_zero_without_calling_upsert(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        monkeypatch.setattr(reader, "get_metric_history", lambda *a, **k: [])
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("x")))
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda *a, **k: pytest.fail("must not call upsert with zero rows"),
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        assert agg._write_macro_monthly_append() == 0


# ---------------------------------------------------------------------------
# main() call-site wiring
# ---------------------------------------------------------------------------


def test_main_calls_macro_monthly_append_when_supabase_enabled(tmp_path, monkeypatch):
    from tests.test_aggregator import _build_data_tree

    data_dir, cfg_path = _build_data_tree(tmp_path)
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", data_dir / "latest.json")
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")

    import utils.supabase_writer as sw

    monkeypatch.setattr(sw, "upsert_metric_history", lambda **k: len(k.get("data", {})))
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_history_monthly", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_definitions_monthly", lambda *a, **k: 0)

    calls = []
    monkeypatch.setattr(agg, "_write_macro_monthly_append", lambda: (calls.append(1), 0)[1])

    exit_code = agg.main()
    assert exit_code == 0
    assert calls == [1]


def test_main_notifies_distinctly_on_macro_monthly_append_write_failure(tmp_path, monkeypatch):
    """Mirrors D5's own test: a SupabaseWriteError from the macro append must
    notify with a message distinguishable from the daily metric_history
    failure AND the D5 reserves-split failure -- three tables, three
    responder actions."""
    from tests.test_aggregator import _build_data_tree

    data_dir, cfg_path = _build_data_tree(tmp_path)
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", data_dir / "latest.json")
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")

    import utils.supabase_writer as sw

    monkeypatch.setattr(sw, "upsert_metric_history", lambda **k: len(k.get("data", {})))
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_history_monthly", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_definitions_monthly", lambda *a, **k: 0)

    def _raise(*a, **k):
        raise sw.SupabaseWriteError("simulated outage")

    monkeypatch.setattr(agg, "_write_macro_monthly_append", _raise)

    notify_calls = []
    monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

    exit_code = agg.main()
    assert exit_code == 0  # swallow-and-continue, matching D5's own contract
    assert any("macro monthly append" in title.lower() for _level, title in notify_calls)
    # Must be distinguishable from the D5 reserves-split failure title.
    assert not any(title == "aggregate — Supabase monthly write failed" for _level, title in notify_calls)
