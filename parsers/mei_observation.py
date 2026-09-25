"""Table-scoped observation extraction for Bangladesh Bank MEI PDFs.

Unlike the legacy line parser, this selects a row and its dated column from
the same extracted table. A cover date alone is never treated as the value's
period.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.periods import fy_month_end, parse_fy_end, parse_month_year
from parsers.registry import register

_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?![\w.])")
_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1
)}
_HEADINGS = {
    "money_credit": "money and credit developments",
    "reserve_money": "reserve money developments",
    "deficit_financing": "government deficit financing",
    "imports_lc": "custom-based import",
}


def _norm(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _numeric(value: object) -> float:
    match = _NUMBER.search(str(value or "").replace("\n", " "))
    if not match:
        raise ParseError(f"selected MEI cell has no number: {value!r}")
    return float(match.group().replace(",", ""))


def _release_status(header: str) -> str:
    if re.search(r"(?:\d{4}|\bFY\s*\d{2})P\b", header, re.IGNORECASE):
        return "provisional"
    if re.search(r"(?:\d{4}|\bFY\s*\d{2})R\b", header, re.IGNORECASE):
        return "final"
    return "unknown"


def _header_rows(table: list[list[object]]) -> list[tuple[int, list[object]]]:
    return [
        (index, row) for index, row in enumerate(table)
        if any(
            "particulars" in _norm(cell)
            or _norm(cell) in {"month", "fy"}
            for cell in row
        )
    ]


def _row_index(table: list[list[object]], label: str, *, strategy: str | None = None) -> tuple[int, date | None]:
    needle = _norm(label)
    matches: list[tuple[int, date | None]] = []
    for index, row in enumerate(table):
        first = _norm(row[0]) if row else ""
        next_first = _norm(table[index + 1][0]) if index + 1 < len(table) and table[index + 1] else ""
        joined = f"{first} {next_first}".strip()
        if strategy == "latest_fy":
            if not re.fullmatch(
                r"(?:fy\s*\d{2}[PR]?|july\s*[-–]\s*may\s+of\s+fy\s*\d{2}[PR]?)",
                first,
                re.IGNORECASE,
            ):
                continue
            fy = parse_fy_end(first)
            if fy is None:
                continue
            # A complete FY row ends 30 June. A July-May row ends 31 May;
            # it keeps its actual partial-period end, even when FY26 is latest.
            if re.search(r"\bjuly\s*[-–]\s*may\b", first):
                period = fy_month_end(fy, 5)
            else:
                period = fy_month_end(fy, 6)
            matches.append((index, period))
        elif first and (needle in first or needle in joined):
            matches.append((index, None))
    if not matches:
        raise ParseError(f"MEI row {label!r} not found")
    if strategy == "latest_fy":
        newest = max(period for _, period in matches if period)
        latest = [(index, period) for index, period in matches if period == newest]
        if len(latest) != 1:
            raise ParseError(f"ambiguous latest MEI fiscal row for {label!r}")
        return latest[0]
    if len(matches) != 1:
        raise ParseError(f"ambiguous MEI row {label!r}")
    return matches[0]


def _unit_in_table(table: list[list[object]], unit: str, page_text: str = "") -> bool:
    text = _norm(" ".join(str(cell or "") for row in table for cell in row) + " " + page_text)
    if unit == "BDT crore cumulative":
        unit = "BDT crore"
    if unit == "ratio":
        # The ratio row is dimensionless even though the same reserve-money
        # table's balances are headed BDT crore.
        return "money multiplier" in text
    if unit == "BDT crore":
        return "bdt in crore" in text
    if unit == "USD million":
        return "usd in million" in text
    return False


def _select_month_column(table: list[list[object]]) -> tuple[int, date, str]:
    candidates: list[tuple[int, date, str]] = []
    for _, row in _header_rows(table):
        for column, cell in enumerate(row):
            date_end = parse_month_year(str(cell or ""))
            if date_end is not None:
                candidates.append((column, date_end, str(cell)))
    if not candidates:
        raise ParseError("MEI table is missing a dated month header")
    latest_date = max(item[1] for item in candidates)
    latest = [item for item in candidates if item[1] == latest_date]
    if len(latest) != 1:
        raise ParseError(f"ambiguous MEI date header for {latest_date.isoformat()}")
    return latest[0]


def _select_observation(
    table: list[list[object]], *, selector: dict[str, Any], page_text: str = "",
) -> ParseResult:
    """Select a value and its unit/release marker/period from one table."""
    row_selector = str(selector.get("row", ""))
    column_selector = str(selector.get("column", ""))
    unit = str(selector.get("unit", ""))
    if not _unit_in_table(table, unit, page_text):
        raise ParseError(f"MEI table unit does not prove {unit!r}")

    if column_selector == "latest_month":
        column, period, header = _select_month_column(table)
        row_index, _ = _row_index(table, row_selector)
    elif column_selector in {"latest_fy", "banking system", "non-bank", "Total net domestic financing", "Net foreign financing"}:
        row_index, period = _row_index(
            table, row_selector or "FY", strategy="latest_fy",
        )
        head_row = next((
            row for _, row in _header_rows(table)
            if any("particulars" in _norm(c) for c in row)
            or (row and _norm(row[0]) == "fy")
        ), None)
        if head_row is None:
            raise ParseError("MEI deficit table is missing its column header")
        if column_selector == "latest_fy":
            raise ParseError("MEI fiscal selector requires a named financing column")
        matches = [
            i for i, cell in enumerate(head_row)
            if column_selector.casefold() in _norm(cell)
        ]
        if len(matches) != 1:
            raise ParseError(f"ambiguous or missing MEI column {column_selector!r}")
        column = matches[0]
        header = str(head_row[column] or "")
    elif column_selector in {"Import LCs opening", "Import LCs settlement"}:
        period, row_index, column, header = _latest_lc_selection(table, column_selector)
    else:
        raise ParseError(f"unsupported MEI column selector {column_selector!r}")

    try:
        raw = table[row_index][column]
    except (IndexError, TypeError) as exc:
        raise ParseError("MEI selected row/column is outside the table") from exc
    row_context = str(table[row_index][0] or "") if table[row_index] else ""
    status = _release_status(f"{header} {row_context}")
    return ParseResult(
        value=_numeric(raw), _parse_strategy="mei_observation", source_as_of=period,
        unit=unit, release_status=status,
    )


def _latest_lc_selection(table: list[list[object]], column_name: str) -> tuple[date, int, int, str]:
    top_rows = [row for row in table[:4] if row]
    column_header_candidates = [
        i for row in top_rows for i, cell in enumerate(row)
        if _norm(cell) == _norm(column_name)
    ]
    columns = sorted(set(column_header_candidates))
    if len(columns) != 1:
        raise ParseError(f"ambiguous or missing MEI LC column {column_name!r}")
    column = columns[0]
    target_year: int | None = None
    target_header = ""
    for row in table[:8]:
        if row and _norm(row[0]) == "month" and column < len(row):
            fy = parse_fy_end(str(row[column] or ""))
            if fy is not None and (target_year is None or fy > target_year):
                target_year = fy
                target_header = str(row[column])
    if target_year is None:
        raise ParseError("MEI LC table is missing a fiscal-year column header")
    month_rows: list[tuple[int, int]] = []
    for row_index, row in enumerate(table):
        label = _norm(row[0]) if row else ""
        month = _MONTHS.get(label)
        if month:
            month_rows.append((row_index, month))
    if not month_rows:
        raise ParseError("MEI LC table is missing monthly rows")
    row_index, month = max(month_rows, key=lambda item: (item[1] - 7) % 12)
    period = fy_month_end(target_year, month)
    return period, row_index, column, target_header


def _instruction(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ParseError("MEI observation instruction must be JSON") from exc
    if not isinstance(parsed, dict) or not {"table", "row", "column", "unit"} <= parsed.keys():
        raise ParseError("MEI observation instruction needs table, row, column and unit")
    return parsed


@register("mei_observation")
class MeiObservationParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        selector = _instruction(instruction)
        heading = _HEADINGS.get(str(selector["table"]))
        if heading is None:
            raise ParseError(f"unknown MEI table selector {selector['table']!r}")
        matches: list[ParseResult] = []
        with pdfplumber.open(artifact.artifact_path) as pdf:
            for page in pdf.pages:
                page_text = _norm(page.extract_text() or "")
                if heading not in page_text:
                    continue
                for table in page.extract_tables():
                    try:
                        result = _select_observation(
                            table, selector=selector, page_text=page.extract_text() or "",
                        )
                    except ParseError:
                        continue
                    matches.append(result)
        if not matches:
            raise ParseError(f"no MEI table matched selector {selector!r}")
        if len(matches) != 1:
            raise ParseError(f"ambiguous MEI table for selector {selector!r}")
        return matches[0]
