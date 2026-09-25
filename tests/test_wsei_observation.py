"""Source-period contracts for BB's 20 September 2026 WSEI issue."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.wsei_observation import WseiObservationParser

ROOT = Path(__file__).resolve().parents[1]
WSEI = ROOT / "tests/_pdfs/bb_wsei_2026_09_20.pdf"


def artifact(path: Path = WSEI) -> FetchResult:
    return FetchResult(
        indicator_id="test_wsei", artifact_path=path, artifact_type="pdf",
        fetched_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        source_url="https://www.bb.org.bd/wsei.pdf", sha256="0" * 64, cache_hit=False,
    )


def parse(**values: object):
    return WseiObservationParser().parse(artifact(), json.dumps(values))


@pytest.mark.parametrize(
    ("selector", "expected", "period", "unit"),
    [
        ({"series": "remittance", "row": "Wage Earners' Remittances", "column": "August, 2026", "line": 0}, 2.97, date(2026, 8, 31), "USD billion"),
        ({"series": "remittance", "row": "Wage Earners' Remittances", "column": "FY26P", "line": 0}, 35.59, date(2026, 6, 30), "USD billion"),
        ({"series": "tax", "row": "Tax Revenue (NBR)", "column": "FY26", "line": 0}, 415_473.0, date(2026, 6, 30), "BDT crore"),
        ({"series": "money", "row": "Broad Money (M2)", "column": "July, 2026", "line": 0}, 2_422_923.9, date(2026, 7, 31), "BDT crore"),
        ({"series": "money", "row": "Reserve Money (RM)", "column": "July, 2026", "line": 0}, 463_461.4, date(2026, 7, 31), "BDT crore"),
        ({"series": "imports", "row": "b) Import(f.o.b)", "column": "latest_month", "line": 0}, 6.44, date(2026, 7, 31), "USD billion"),
        ({"series": "exports", "row": "a) Export (f.o.b)", "column": "latest_month", "line": 0}, 4.35, date(2026, 7, 31), "USD billion"),
        ({"series": "exports", "row": "a) Export (f.o.b)", "column": "latest_complete_fy", "line": 0}, 43.86, date(2026, 6, 30), "USD billion"),
        ({"series": "nsc", "row": "b) Total Outstanding", "column": "latest_month", "line": 1}, 336_061.82, date(2026, 7, 31), "BDT crore"),
        ({"series": "credit", "row": "c) Credit to the Private Sector", "column": "latest_month", "line": 3}, 1_823_101.6, date(2026, 7, 31), "BDT crore"),
        ({"series": "lc", "row": "Total", "column": "July, FY27", "subcolumn": "Opening", "line": 9, "scale": 1000}, 6_901.7, date(2026, 7, 31), "USD million"),
        ({"series": "lc", "row": "Total", "column": "July, FY27", "subcolumn": "Settlement", "line": 9, "scale": 1000}, 6_279.3, date(2026, 7, 31), "USD million"),
    ],
)
def test_wsei_value_period_and_unit_come_from_the_selected_component(selector, expected, period, unit):
    result = parse(**selector, unit=unit)
    assert (result.value, result.source_as_of, result.unit) == (expected, period, unit)


def test_wsei_selects_each_components_own_date_not_cover_date():
    result = parse(
        series="remittance", row="Wage Earners' Remittances", column="August, 2026",
        line=0, unit="USD billion",
    )
    assert result.source_as_of == date(2026, 8, 31)
    assert result.source_as_of != date(2026, 9, 20)


def test_two_matching_component_periods_are_rejected():
    from parsers.wsei_observation import _select_observation

    table = [
        ["5.", "", ""],
        ["Billion US$", "17 September 2026", "17 September 2026"],
        ["", "Wage Earners' Remittances", ""],
        [None, "2.97", "3.01"],
    ]
    with pytest.raises(ParseError, match="ambiguous"):
        _select_observation(table, selector={
            "series": "remittance", "row": "Wage Earners' Remittances",
            "column": "17 September 2026", "line": 0, "unit": "USD billion",
        })


def test_missing_period_header_is_rejected_even_if_value_exists():
    from parsers.wsei_observation import _select_observation

    table = [["9.", ""], ["BDT in crore", "FY26P"], ["", "Tax Revenue"], ["", "415473"]]
    with pytest.raises(ParseError, match="header"):
        _select_observation(table, selector={
            "series": "tax", "row": "Tax Revenue", "column": "June, 2026",
            "line": 0, "unit": "BDT crore",
        })
