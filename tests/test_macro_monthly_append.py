"""Tests for aggregate_latest._write_macro_monthly_append and its helpers
(2026-08-08 frozen-charts incident, AGENTS.md landmine 50).

Covers: the CPI-trio pure transform (mapping, append-only skip, month-end
vintage check, the closed-month guard, the general==p2p wrong-column
equality guard -- both fail-closed and its June-2026 regression, range
check), the remittance HTML parser (real fixture + synthetic FY-boundary
case, header-resolved value column, structurally-empty-parse guard), row
selection (range/skip-if-exists/future-date guard), and the top-level
orchestrator wired against mocked reader/writer/fetch so no real network or
Supabase call goes out.

2026-08-08 Opus adversarial review (FIX FIRST verdict) added: H2 closed-
month regression, H3 structurally-empty-parse + 0-rows/0-reasons warning,
H4 header-resolved value column, M2 future as_of rejection, M3 no-fallback-
table, M4 distinct existing-rows-read failure, M6 fetch-skip gating, L6
fail-closed equality guard, L4 fail-safe/crash-containment tests.
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

# Fixed reference date for every test below -- deterministic, independent of
# the real wall clock. All existing daily/monthly fixtures below (June 2026
# and earlier) are comfortably CLOSED relative to this date.
TODAY = date(2026, 8, 8)

# Real grouped-header shape (see tests/fixtures/bb_wageremitance.html) --
# reused by every synthetic table fixture below so H4's header-resolved
# value-column lookup has something to resolve against.
_REMIT_THEAD = (
    '<thead><tr><td rowspan="2">Year/Month</td><td colspan="2">Remittances</td></tr>'
    "<tr><th>In million US dollar</th><th>In billion Taka</th></tr></thead>"
)


@pytest.fixture(autouse=True)
def _deny_real_imports_pdf_fetch_by_default(monkeypatch):
    """PR-C: _write_macro_monthly_append grew an imports sub-path
    (_fetch_imports_mei_pdf) that is NOT gated the way the remittance
    sub-path is (M6's "skip the browser if the previous month already
    exists" optimization doesn't apply the same way here, since imports has
    a genuine ~2-month structural lag -- see AGENTS.md). Most tests in this
    file mock get_metric_history_monthly to unconditionally return [],
    which would otherwise make the imports sub-path proceed straight to a
    REAL network fetch of BB's MEI PDF on every single test in this class --
    exactly the "hermetic test suite" violation landmine 30 warns about
    (confirmed live: this fired a real SSL handshake to bb.org.bd before
    this fixture existed). Every test that DOES care about the imports
    sub-path re-patches this locally, the same way individual tests already
    re-patch _fetch_remittance_html to exercise the remittance sub-path.
    """
    monkeypatch.setattr(
        agg, "_fetch_imports_mei_pdf",
        lambda: (_ for _ in ()).throw(FetchError("autouse default -- not mocked in this test")),
    )


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
            today=TODAY,
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
            food_row=None, nonfood_row=None,
            p2p_row=(1.23, date(2026, 6, 30)),  # differs from general -> no equality guard
            existing_pairs=set(),
            today=TODAY,
        )
        assert rows[0]["as_of"] == "2026-06-01"
        assert rows[0]["source_as_of"] == "2026-06-30"  # true recovered vintage, not day-1

    def test_missing_daily_row_is_skipped_with_a_reason(self):
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=None, food_row=None, nonfood_row=None, p2p_row=None,
            existing_pairs=set(),
            today=TODAY,
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
            today=TODAY,
        )
        assert rows == []
        assert "not a month-end vintage" in reasons[0]

    # --- H2: closed-month guard (2026-08-08 Opus review) --------------------

    def test_h2_regression_2026_08_31_carried_forward_as_of_is_rejected(self):
        """The exact scenario the reviewer proved: today=2026-08-31 and a
        run-date-forged as_of=2026-08-31 -- August has 31 days, so the
        forged date COINCIDENTALLY equals August's real month-end and would
        pass the month-end check alone. The closed-month guard must still
        reject it because it describes the CURRENT, not-yet-closed month."""
        today = date(2026, 8, 31)
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 8, 31)),
            food_row=None, nonfood_row=None, p2p_row=None,
            existing_pairs=set(),
            today=today,
        )
        assert rows == []
        assert "not-yet-closed" in reasons[0] or "closed-month" in reasons[0]

    def test_h2_a_genuinely_closed_month_still_passes_on_the_same_date(self):
        """Same today=2026-08-31, but the daily row describes JULY (already
        closed) -- the guard must not reject legitimate closed-month data."""
        today = date(2026, 8, 31)
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 7, 31)),
            food_row=None, nonfood_row=None,
            p2p_row=(1.23, date(2026, 7, 31)),
            existing_pairs=set(),
            today=today,
        )
        assert {r["metric_id"] for r in rows} == {"cpi_12m_avg_monthly"}
        assert not any("closed-month" in r for r in reasons)

    @pytest.mark.parametrize("bad_value", [-1.0, 0.0, 30.0, 45.2])
    def test_range_check_rejects_outside_0_to_30(self, bad_value):
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=(bad_value, date(2026, 6, 30)),
            food_row=None, nonfood_row=None, p2p_row=None,
            existing_pairs=set(),
            today=TODAY,
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
            today=TODAY,
        )
        by_id = {r["metric_id"]: r for r in rows}
        assert "cpi_12m_avg_monthly" not in by_id
        assert "cpi_p2p_food_monthly" in by_id
        assert "cpi_p2p_nonfood_monthly" in by_id
        assert any("exactly equals" in r for r in reasons)

    def test_general_differing_from_p2p_is_not_guarded(self):
        """A genuine (non-equal) general_inflation reading must NOT be
        blocked by the guard -- it only fires on exact equality."""
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=None, nonfood_row=None,
            p2p_row=(9.16, date(2026, 6, 30)),  # different value -> no guard
            existing_pairs=set(),
            today=TODAY,
        )
        assert {r["metric_id"] for r in rows} == {"cpi_12m_avg_monthly"}
        assert not any("exactly equals" in r for r in reasons)

    def test_equality_guard_requires_matching_as_of(self):
        """Equal VALUES on DIFFERENT months is coincidence, not the wrong-
        column defect -- the guard must compare same-as_of only."""
        rows, _reasons = agg._cpi_monthly_append_rows(
            general_row=(9.16, date(2026, 6, 30)),
            food_row=None, nonfood_row=None,
            p2p_row=(9.16, date(2026, 5, 31)),  # same value, different month
            existing_pairs=set(),
            today=TODAY,
        )
        assert {r["metric_id"] for r in rows} == {"cpi_12m_avg_monthly"}

    # --- L6: fail-CLOSED when point_to_point_inflation is unavailable -------

    def test_l6_general_write_skipped_when_p2p_unavailable_fail_closed(self):
        """2026-08-08 Opus review L6: the equality guard previously failed
        OPEN (wrote general_inflation unverified) when p2p_row was None. It
        must fail CLOSED instead -- cannot verify the guard, so don't write."""
        rows, reasons = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=(8.60, date(2026, 6, 30)),
            nonfood_row=(9.61, date(2026, 6, 30)),
            p2p_row=None,
            existing_pairs=set(),
            today=TODAY,
        )
        by_id = {r["metric_id"]: r for r in rows}
        assert "cpi_12m_avg_monthly" not in by_id
        # food/non-food don't depend on the guard at all -- unaffected.
        assert "cpi_p2p_food_monthly" in by_id
        assert "cpi_p2p_nonfood_monthly" in by_id
        assert any("cannot verify" in r or "fail-closed" in r for r in reasons)

    def test_append_only_skips_existing_pair(self):
        """The backfill (scripts/backfill_monthly_chart_series.py) already
        wrote cpi_12m_avg_monthly for 2026-06-01 with an official value --
        the appender must NEVER clobber it with a re-derived daily value."""
        rows, _reasons = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=None, nonfood_row=None,
            p2p_row=(1.23, date(2026, 6, 30)),
            existing_pairs={("cpi_12m_avg_monthly", date(2026, 6, 1))},
            today=TODAY,
        )
        assert rows == []

    def test_writes_all_available_trio_siblings_in_one_batch(self):
        rows, _ = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=(8.60, date(2026, 6, 30)),
            nonfood_row=(9.61, date(2026, 6, 30)),
            p2p_row=(9.16, date(2026, 6, 30)),
            existing_pairs=set(),
            today=TODAY,
        )
        assert len(rows) == 3
        assert {r["as_of"] for r in rows} == {"2026-06-01"}  # aligned, same run

    def test_source_label_is_econdelta_daily_cpi(self):
        rows, _ = agg._cpi_monthly_append_rows(
            general_row=(8.68, date(2026, 6, 30)),
            food_row=None, nonfood_row=None,
            p2p_row=(1.23, date(2026, 6, 30)),
            existing_pairs=set(),
            today=TODAY,
        )
        assert rows[0]["source"] == "econdelta_daily_cpi"


# ---------------------------------------------------------------------------
# parse_remittance_table — real fixture + synthetic FY-boundary/edge cases
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
        html = f"""
        <table id="sortableTable">{_REMIT_THEAD}<tbody>
          <tr><td colspan="3">2026-2027</td></tr>
          <tr><td>July</td><td>2900.00</td><td>355.00</td></tr>
        </tbody></table>
        """
        parsed = dict(agg.parse_remittance_table(html))
        assert parsed[date(2026, 7, 1)] == pytest.approx(2900.00)

    def test_no_table_raises_value_error(self):
        with pytest.raises(ValueError, match="sortableTable"):
            agg.parse_remittance_table("<html><body>nothing here</body></html>")

    def test_decoy_table_without_sortable_id_is_not_used(self):
        """2026-08-08 Opus review M3: a page carrying some OTHER table (not
        id="sortableTable") must NOT be silently parsed as a fallback -- the
        prior `soup.find("table", id=...) or soup.find("table")` fallback
        would have picked up a decoy table like this one."""
        html = f"""
        <table id="decoyTable">{_REMIT_THEAD}<tbody>
          <tr><td colspan="3">2026-2027</td></tr>
          <tr><td>July</td><td>9999.99</td><td>1.00</td></tr>
        </tbody></table>
        """
        with pytest.raises(ValueError, match="sortableTable"):
            agg.parse_remittance_table(html)

    def test_no_tbody_raises_value_error(self):
        with pytest.raises(ValueError, match="no <tbody>"):
            agg.parse_remittance_table('<table id="sortableTable"></table>')

    def test_no_thead_raises_value_error(self):
        """H4: without a <thead> to resolve the value column from, refuse
        rather than guess a position."""
        html = '<table id="sortableTable"><tbody><tr><td>x</td></tr></tbody></table>'
        with pytest.raises(ValueError, match="<thead>"):
            agg.parse_remittance_table(html)

    def test_h4_value_column_resolved_by_header_text_not_position(self):
        """2026-08-08 Opus review H4: an INSERTED column before the USD
        value column must not silently shift a hardcoded cells[1] onto the
        wrong data -- the real column must still be found by its header
        text ("million US dollar"), wherever it actually sits."""
        html = """
        <table id="sortableTable">
          <thead>
            <tr><td rowspan="2">Year/Month</td><td rowspan="2">Cumulative</td>
                <td colspan="2">Remittances</td></tr>
            <tr><th>In million US dollar</th><th>In billion Taka</th></tr>
          </thead>
          <tbody>
            <tr><td colspan="4">2026-2027</td></tr>
            <tr><td>July</td><td>99999.00</td><td>2950.00</td><td>360.00</td></tr>
          </tbody>
        </table>
        """
        parsed = dict(agg.parse_remittance_table(html))
        # The inserted "Cumulative" column sits at data-row index 1; the
        # real USD value (2950.00) is at index 2 -- a hardcoded cells[1]
        # would have wrongly returned 99999.00 (still numeric, still
        # in-range-looking, but permanently wrong).
        assert parsed[date(2026, 7, 1)] == pytest.approx(2950.00)

    def test_r2_two_matching_usd_columns_raises_instead_of_guessing(self):
        """2026-08-08 re-review, finding R2: the H4 fix returned the FIRST
        header cell matching "million US dollar" -- a table with TWO such
        columns (e.g. a cumulative-FYTD column and a monthly column, both
        labelled "... million US dollar") would silently pick whichever
        came first in grid order, re-opening H4 under a different disguise
        (still in-range, still plausible, still permanently wrong). Must
        raise on ambiguity, never guess."""
        html = """
        <table id="sortableTable">
          <thead>
            <tr><td rowspan="2">Year/Month</td>
                <td colspan="2">Cumulative</td><td colspan="2">Monthly</td></tr>
            <tr><th>Cumulative (in million US dollar)</th><th>Cumulative BDT</th>
                <th>Monthly (in million US dollar)</th><th>Monthly BDT</th></tr>
          </thead>
          <tbody>
            <tr><td colspan="5">2026-2027</td></tr>
            <tr><td>July</td><td>18500.00</td><td>2260.00</td>
                <td>2950.00</td><td>360.00</td></tr>
          </tbody>
        </table>
        """
        with pytest.raises(ValueError, match="ambiguous"):
            agg.parse_remittance_table(html)

    def test_r2_single_matching_usd_column_still_resolves_normally(self):
        """Sanity check: the R2 fix's ambiguity guard must not fire on the
        normal single-match case (regression guard against over-tightening)."""
        parsed = dict(agg.parse_remittance_table(f"""
        <table id="sortableTable">{_REMIT_THEAD}<tbody>
          <tr><td colspan="3">2026-2027</td></tr>
          <tr><td>July</td><td>2950.00</td><td>360.00</td></tr>
        </tbody></table>
        """))
        assert parsed[date(2026, 7, 1)] == pytest.approx(2950.00)

    def test_h3_structurally_empty_parse_raises(self):
        """2026-08-08 Opus review H3: the newest FY block's header row
        rendered as <th> instead of a single colspan <td> -- fy_start_year
        is never set for that block, so its 12 data rows (which DO still
        use <td>) are silently skipped too. If this happens to EVERY block
        in the table, the parse returns ZERO rows -- must raise, not return
        an empty list silently."""
        html = f"""
        <table id="sortableTable">{_REMIT_THEAD}<tbody>
          <tr><th colspan="3">2026-2027</th></tr>
          <tr><td>July</td><td>2900.00</td><td>355.00</td></tr>
        </tbody></table>
        """
        with pytest.raises(ValueError, match="ZERO rows"):
            agg.parse_remittance_table(html)

    def test_unparseable_value_cell_is_skipped_not_crashed(self):
        html = f"""
        <table id="sortableTable">{_REMIT_THEAD}<tbody>
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
        rows, _ = agg._select_new_remittance_rows(parsed, existing_as_of=set(), today=TODAY)
        assert {r["as_of"] for r in rows} == {"2026-07-01"}

    def test_skip_if_exists_append_only(self):
        parsed = [(date(2026, 7, 1), 2950.0), (date(2026, 8, 1), 3010.0)]
        rows, _ = agg._select_new_remittance_rows(
            parsed, existing_as_of={date(2026, 7, 1)}, today=TODAY,
        )
        assert {r["as_of"] for r in rows} == {"2026-08-01"}

    # --- M2: reject future as_of -------------------------------------------

    def test_m2_future_as_of_from_corrupted_fy_header_is_rejected(self):
        """2026-08-08 Opus review M2: a corrupted/future FY header (e.g. a
        stray "2030-2031" block) must not write a nonsense future as_of --
        BB cannot have published a month that hasn't happened yet."""
        parsed = [(date(2030, 7, 1), 3000.0)]
        rows, reasons = agg._select_new_remittance_rows(
            parsed, existing_as_of=set(), today=TODAY,
        )
        assert rows == []
        assert "future" in reasons[0]

    def test_m2_the_current_closed_month_boundary_is_accepted(self):
        """as_of exactly at today's month-start is the boundary -- not
        rejected (a future check should reject STRICTLY future months, not
        the current one, which the CPI closed-month guard handles
        separately for its own daily-derivation risk)."""
        parsed = [(TODAY.replace(day=1), 3000.0)]
        rows, reasons = agg._select_new_remittance_rows(
            parsed, existing_as_of=set(), today=TODAY,
        )
        assert len(rows) == 1
        assert reasons == []

    @pytest.mark.parametrize("bad_value", [499.99, 6000.01, -100.0])
    def test_value_range_rejects_outside_500_to_6000(self, bad_value):
        parsed = [(date(2026, 7, 1), bad_value)]
        rows, reasons = agg._select_new_remittance_rows(parsed, existing_as_of=set(), today=TODAY)
        assert rows == []
        assert "outside" in reasons[0]

    def test_boundary_values_are_accepted(self):
        parsed = [(date(2026, 7, 1), 500.0), (date(2026, 8, 1), 6000.0)]
        rows, reasons = agg._select_new_remittance_rows(parsed, existing_as_of=set(), today=TODAY)
        assert len(rows) == 2
        assert reasons == []

    def test_source_label_is_bb_wageremitance(self):
        rows, _ = agg._select_new_remittance_rows(
            [(date(2026, 7, 1), 2950.0)], existing_as_of=set(), today=TODAY,
        )
        assert rows[0]["source"] == "bb_wageremitance"
        assert rows[0]["metric_id"] == "remittance_usd_mn_monthly"


# ---------------------------------------------------------------------------
# _previous_month_start (M6 helper)
# ---------------------------------------------------------------------------


class TestPreviousMonthStart:
    def test_mid_month(self):
        assert agg._previous_month_start(date(2026, 8, 8)) == date(2026, 7, 1)

    def test_first_of_month(self):
        assert agg._previous_month_start(date(2026, 8, 1)) == date(2026, 7, 1)

    def test_january_rolls_back_to_prior_december(self):
        assert agg._previous_month_start(date(2026, 1, 15)) == date(2025, 12, 1)


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
        # remittance sub-path fails gracefully (fetch stubbed to fail) so
        # only CPI rows reach the final upsert. (L5: single monkeypatch --
        # the prior version had a dead first patch immediately overwritten.)
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("x")))

        captured: list[dict] = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 3
        ids = {r["metric_id"] for r in captured}
        assert ids == {"cpi_12m_avg_monthly", "cpi_p2p_food_monthly", "cpi_p2p_nonfood_monthly"}

    def test_cpi_read_failure_notifies_and_does_not_crash(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def raise_read(*a, **k):
            raise SupabaseReadError("boom")

        monkeypatch.setattr(reader, "get_metric_history", raise_read)
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("x")))
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: pytest.fail("no rows expected"))

        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 0
        assert any("CPI read failed" in title for _level, title in notify_calls)

    def test_m1_non_supabase_read_error_from_json_decode_is_contained(self, monkeypatch):
        """2026-08-08 Opus review M1: requests' JSONDecodeError (a 200-with-
        HTML-body PostgREST/CDN incident) is NOT a SupabaseReadError and
        would have escaped the old `except SupabaseReadError` uncaught,
        crashing the whole aggregate run. The broadened `except Exception`
        must contain it exactly like a real SupabaseReadError."""
        import json as json_module

        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def raise_json_decode_error(*a, **k):
            raise json_module.JSONDecodeError("Expecting value", "<html>oops</html>", 0)

        monkeypatch.setattr(reader, "get_metric_history", raise_json_decode_error)
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("x")))
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: pytest.fail("no rows expected"))
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        # Must not raise -- this call itself IS the assertion.
        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 0

    # --- L4(a): a failing existing-rows read BLOCKS the CPI write ----------

    def test_l4a_failing_existing_rows_read_blocks_cpi_write_fail_safe(self, monkeypatch):
        """The append-only fail-safe: if we can't verify what already exists
        in metric_history_monthly, we must NOT write -- even though valid
        daily CPI data is available. A partial existing_cpi (built from only
        SOME of the 3 monthly ids before the read fails) must never be used
        to justify a write; the whole CPI sub-path aborts atomically."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def fake_get_metric_history(metric_id, *, days, **kwargs):
            values = {
                "general_inflation": 8.68, "food_inflation": 8.60,
                "non_food_inflation": 9.61, "point_to_point_inflation": 9.16,
            }
            return self._cpi_daily_row(values[metric_id], date(2026, 6, 30))

        call_count = {"n": 0}

        def flaky_get_metric_history_monthly(metric_id, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise SupabaseReadError("mid-loop failure")
            return []

        monkeypatch.setattr(reader, "get_metric_history", fake_get_metric_history)
        monkeypatch.setattr(reader, "get_metric_history_monthly", flaky_get_metric_history_monthly)
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("x")))
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda *a, **k: pytest.fail("append-only fail-safe: must not write when existing-rows check failed"),
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 0

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

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n >= 1  # cpi_12m_avg_monthly (food/nonfood share the same stub value, still fine)
        assert any("remittance fetch/parse failed" in title for _level, title in notify_calls)
        assert all(r["metric_id"] != "remittance_usd_mn_monthly" for r in captured)

    # --- M4: existing-rows read failure gets its OWN distinct message ------

    def test_m4_remittance_existing_rows_read_failure_has_distinct_message(self, monkeypatch):
        """2026-08-08 Opus review M4: a SupabaseReadError checking
        metric_history_monthly for the remittance id must notify with a
        message about the READ failing -- NOT be misdiagnosed as "could not
        fetch or parse BB's page" (a completely different incident class).
        The browser fetch must never even be attempted in this case."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            if metric_id == agg._REMITTANCE_MONTHLY_ID:
                raise SupabaseReadError("existing-rows read boom")
            return []

        monkeypatch.setattr(reader, "get_metric_history", lambda *a, **k: [])
        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(
            agg, "_fetch_remittance_html",
            lambda: pytest.fail("M4: browser fetch must not be attempted when the existing-rows read failed"),
        )
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: pytest.fail("no rows expected"))

        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title, msg)))

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 0
        remit_read_calls = [c for c in notify_calls if "remittance read failed" in c[1]]
        assert remit_read_calls, notify_calls
        # Scoped to REMITTANCE specifically -- PR-C's independent imports
        # sub-path (autouse-defaulted to fail in this file, see
        # _deny_real_imports_pdf_fetch_by_default) also fires its own,
        # unrelated "imports fetch/parse failed" notify in this scenario;
        # a blanket "no title contains fetch/parse failed" check would be a
        # false positive against that unrelated, correctly-independent leg.
        assert not any(
            "remittance" in title and "fetch/parse failed" in title
            for _level, title, _msg in notify_calls
        )

    def test_r1_non_supabase_read_error_on_existing_rows_check_does_not_discard_cpi_rows(
        self, monkeypatch,
    ):
        """2026-08-08 re-review, finding R1: the remittance existing-rows
        read was still `except SupabaseReadError` while the CPI block above
        (M1) was broadened to `except Exception`. A JSONDecodeError there
        (same 200-with-HTML-body class M1 fixed) would escape THIS narrower
        except, aborting _write_macro_monthly_append with an unhandled
        exception BEFORE its final upsert call -- discarding the CPI trio's
        already-computed rows_to_write even though the CPI sub-path
        succeeded cleanly. Proves BOTH: the function doesn't crash, AND the
        valid CPI rows still reach the upsert."""
        import json as json_module

        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def fake_get_metric_history(metric_id, *, days, **kwargs):
            values = {
                "general_inflation": 8.68, "food_inflation": 8.60,
                "non_food_inflation": 9.61, "point_to_point_inflation": 9.16,
            }
            return self._cpi_daily_row(values[metric_id], date(2026, 6, 30))

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            if metric_id == agg._REMITTANCE_MONTHLY_ID:
                raise json_module.JSONDecodeError("Expecting value", "<html>oops</html>", 0)
            return []  # the 3 CPI monthly-id existing-pairs checks succeed

        monkeypatch.setattr(reader, "get_metric_history", fake_get_metric_history)
        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(
            agg, "_fetch_remittance_html",
            lambda: pytest.fail("existing-rows read failed -- fetch must not be attempted"),
        )

        captured: list[dict] = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        # Must not raise -- the JSONDecodeError must be contained.
        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 3
        ids = {r["metric_id"] for r in captured}
        assert ids == {"cpi_12m_avg_monthly", "cpi_p2p_food_monthly", "cpi_p2p_nonfood_monthly"}

    # --- M6: gate the browser launch on the existing-rows check ------------

    def test_m6_fetch_skipped_when_previous_month_already_present(self, monkeypatch):
        """2026-08-08 Opus review M6: if the previous COMPLETE month's
        remittance row is already recorded, the browser launch is skipped
        entirely (~29/30 runs) -- this is the common daily case."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        prev_month = agg._previous_month_start(TODAY)  # 2026-07-01

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            if metric_id == agg._REMITTANCE_MONTHLY_ID:
                return [{"metric_id": metric_id, "as_of": prev_month.isoformat()}]
            return []

        monkeypatch.setattr(reader, "get_metric_history", lambda *a, **k: [])
        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(
            agg, "_fetch_remittance_html",
            lambda: pytest.fail("M6: fetch must be skipped when the previous month already exists"),
        )
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: pytest.fail("no rows expected"))
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 0

    def test_m6_fetch_attempted_when_previous_month_missing(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        monkeypatch.setattr(reader, "get_metric_history", lambda *a, **k: [])
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])

        fetch_calls = {"n": 0}

        def counted_fetch():
            fetch_calls["n"] += 1
            raise FetchError("unreachable")

        monkeypatch.setattr(agg, "_fetch_remittance_html", counted_fetch)
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: pytest.fail("no rows expected"))
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        agg._write_macro_monthly_append(today=TODAY)
        assert fetch_calls["n"] == 1

    def test_remittance_writes_when_fetch_and_parse_succeed(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        monkeypatch.setattr(reader, "get_metric_history", lambda *a, **k: [])
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])

        html = f"""
        <table id="sortableTable">{_REMIT_THEAD}<tbody>
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

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 1
        assert captured[0]["metric_id"] == "remittance_usd_mn_monthly"
        assert captured[0]["as_of"] == "2026-07-01"

    # --- H3: (0 rows, 0 reasons) from a "successful" parse is never silent -

    def test_h3_zero_new_zero_reasons_logs_a_warning(self, monkeypatch, caplog):
        """The parse succeeds (so H3's raise-on-empty doesn't fire) but
        returns only OLD months (before min_as_of) -- _select_new_remittance_rows
        legitimately returns (0 rows, 0 reasons). This is usually just "BB
        hasn't published a new month yet" but is ALSO exactly what a
        partial structural failure (only the newest FY block silently
        dropped) would look like -- must never be silent either way."""
        import logging

        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        monkeypatch.setattr(reader, "get_metric_history", lambda *a, **k: [])
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])

        html = f"""
        <table id="sortableTable">{_REMIT_THEAD}<tbody>
          <tr><td colspan="3">2025-2026</td></tr>
          <tr><td>May</td><td>2969.56</td><td>363.29</td></tr>
        </tbody></table>
        """
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: html)
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: pytest.fail("no rows expected"))
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        with caplog.at_level(logging.WARNING):
            n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 0
        assert "0 were new and 0 were flagged invalid" in caplog.text

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

        assert agg._write_macro_monthly_append(today=TODAY) == 0


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


def test_l4b_non_supabase_write_error_from_appender_does_not_crash_main(tmp_path, monkeypatch):
    """2026-08-08 Opus review L4(b)/M1: the call site's except was broadened
    from `SupabaseWriteError` to `Exception` as a defense-in-depth backstop
    -- some OTHER exception type escaping _write_macro_monthly_append (not
    just a write failure) must still not crash main()."""
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

    def _raise_unexpected(*a, **k):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(agg, "_write_macro_monthly_append", _raise_unexpected)

    notify_calls = []
    monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

    exit_code = agg.main()  # must not raise
    assert exit_code == 0
    assert any("macro monthly append" in title.lower() for _level, title in notify_calls)
