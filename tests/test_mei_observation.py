"""Source-period contracts for the captured June and July 2026 MEI issues."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.mei_observation import MeiObservationParser
from parsers.periods import fy_month_end

ROOT = Path(__file__).resolve().parents[1]
JULY = ROOT / "tests/_pdfs/bb_mei_2026_july.pdf"
JUNE = ROOT / "tests/_pdfs/bb_mei_2026_june.pdf"


def artifact(path: Path) -> FetchResult:
    return FetchResult(
        indicator_id="test_mei", artifact_path=path, artifact_type="pdf",
        fetched_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        source_url="https://www.bb.org.bd/mei.pdf", sha256="0" * 64, cache_hit=False,
    )


def instruction(**values: object) -> str:
    return json.dumps(values)


def parse(path: Path, **values: object):
    return MeiObservationParser().parse(artifact(path), instruction(**values))


@pytest.mark.parametrize(
    ("row", "unit", "expected"),
    [
        ("B. Deposits of the banking system", "BDT crore", 2_079_911.1),
        ("A. Currency outside banks", "BDT crore", 336_375.2),
    ],
)
def test_july_mei_selects_june_stock_column_with_value_period_and_unit(row, unit, expected):
    result = parse(
        JULY, table="money_credit", row=row, column="latest_month", unit=unit,
    )
    assert (result.value, result.source_as_of, result.unit) == (
        expected, date(2026, 6, 30), unit,
    )
    assert result.release_status == "provisional"


@pytest.mark.parametrize(
    ("row", "unit", "expected"),
    [
        ("B. Deposits held with BB", "BDT crore", 106_100.1),
        ("Money multiplier", "ratio", 5.07),
    ],
)
def test_july_mei_reserve_table_uses_its_own_unit_and_period(row, unit, expected):
    result = parse(JULY, table="reserve_money", row=row, column="latest_month", unit=unit)
    assert (result.value, result.source_as_of, result.unit) == (
        expected, date(2026, 6, 30), unit,
    )


@pytest.mark.parametrize(
    ("column", "expected"),
    [("Import LCs opening", 6_198.59), ("Import LCs settlement", 7_116.88)],
)
def test_july_mei_latest_lc_month_excludes_annual_total(column, expected):
    result = parse(
        JULY, table="imports_lc", row="latest_month", column=column, unit="USD million",
    )
    assert (result.value, result.source_as_of, result.unit) == (
        expected, date(2026, 6, 30), "USD million",
    )


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("banking system", 165_538.20),
        ("non-bank", -847.17),
        ("Total net domestic financing", 164_691.03),
        ("Net foreign financing", 57_743.60),
    ],
)
def test_july_mei_financing_selects_fy26_observed_row_not_budget_target(column, expected):
    result = parse(
        JULY, table="deficit_financing", row="latest_fy", column=column,
        unit="BDT crore cumulative",
    )
    assert (result.value, result.source_as_of, result.unit) == (
        expected, date(2026, 6, 30), "BDT crore cumulative",
    )


def test_june_mei_uses_may_columns_and_dynamic_deficit_page():
    deposits = parse(
        JUNE, table="money_credit", row="B. Deposits of the banking system",
        column="latest_month", unit="BDT crore",
    )
    bank = parse(
        JUNE, table="deficit_financing", row="latest_fy", column="banking system",
        unit="BDT crore cumulative",
    )
    assert (deposits.value, deposits.source_as_of, deposits.unit) == (
        2_041_692.7, date(2026, 5, 31), "BDT crore",
    )
    assert (bank.value, bank.source_as_of, bank.unit) == (
        94_158.9, date(2026, 5, 31), "BDT crore cumulative",
    )


def test_fy_month_end_uses_bangladesh_july_to_june_year():
    assert fy_month_end(2026, 7) == date(2025, 7, 31)
    assert fy_month_end(2026, 6) == date(2026, 6, 30)


def test_duplicate_latest_header_is_ambiguous_and_rejected():
    from parsers.mei_observation import _select_observation

    table = [
        ["(BDT in crore)", None, None],
        ["Particulars", "June, 2026P", "June, 2026P"],
        ["Deposits", "100", "101"],
    ]
    with pytest.raises(ParseError, match="ambiguous"):
        _select_observation(table, selector={
            "table": "money_credit", "row": "Deposits", "column": "latest_month",
            "unit": "BDT crore",
        })


def test_missing_header_cannot_be_replaced_by_report_cover_month():
    from parsers.mei_observation import _select_observation

    table = [["(BDT in crore)"], ["Particulars", "Flow FY26P"], ["Deposits", "123"]]
    with pytest.raises(ParseError, match="header"):
        _select_observation(table, selector={
            "table": "money_credit", "row": "Deposits", "column": "latest_month",
            "unit": "BDT crore",
        })


@pytest.mark.parametrize(("suffix", "status"), [("P", "provisional"), ("R", "final")])
def test_deficit_fiscal_row_suffix_preserves_release_status(suffix, status):
    from parsers.mei_observation import _select_observation

    result = _select_observation(
        [["(BDT in crore)", "Particulars", "Banking system"],
         [f"FY26{suffix}", "Borrowing", "42"]],
        selector={
            "table": "deficit_financing", "row": "latest_fy",
            "column": "banking system", "unit": "BDT crore cumulative",
        },
    )
    assert (result.value, result.source_as_of, result.unit, result.release_status) == (
        42.0, date(2026, 6, 30), "BDT crore cumulative", status,
    )
