"""Table-scoped observations from Bangladesh Bank's WSEI PDF."""
from __future__ import annotations

import json
import re
from typing import Any

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.periods import parse_wsei_period
from parsers.registry import register

_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?![\w.])")
_SERIES_HEADINGS = {
    "gdp_growth": "gdp growth rate",
    "remittance": "wage earners' remittances",
    "tax": "tax revenue (nbr)",
    "money": "broad money",
    "lc": "l/c opening and settlement",
    "nsc": "investment in national savings certificates",
    "imports": "import(f.o.b)",
    "exports": "export (f.o.b)",
    "credit": "total domestic credit",
}
_SERIES_SECTION = {
    "gdp_growth": 19, "remittance": 5, "imports": 6, "exports": 7, "tax": 9,
    "nsc": 10, "money": 11, "credit": 12, "lc": 13,
}


def _norm(value: object) -> str:
    return " ".join(str(value or "").replace("’", "'").split()).casefold()


def _line(value: object, number: int, *, role: str) -> str:
    values = str(value or "").splitlines()
    if number < 0 or number >= len(values):
        raise ParseError(f"WSEI {role} line {number} is missing")
    return values[number].strip()


def _number(value: object) -> float:
    match = _NUMBER.search(str(value or "").replace("\n", " "))
    if not match:
        raise ParseError(f"selected WSEI cell has no number: {value!r}")
    return float(match.group().replace(",", ""))


def _release_status(header: str) -> str:
    if re.search(r"(?:\d{4}|\bFY\s*\d{2})P\d*\b", header, re.IGNORECASE):
        return "provisional"
    if re.search(r"(?:\d{4}|\bFY\s*\d{2})R\d*\b", header, re.IGNORECASE):
        return "final"
    return "unknown"


def _unit_proved(table: list[list[object]], unit: str, scale: float) -> bool:
    text = _norm(" ".join(str(cell or "") for row in table for cell in row))
    if unit == "BDT crore":
        return "bdt in crore" in text
    if unit == "USD billion":
        return "billion us$" in text or "billion usd" in text
    if unit == "USD million" and scale == 1000:
        return "billion us$" in text or "billion usd" in text
    return False


def _header_matches(table: list[list[object]], label: str) -> list[tuple[int, int, str]]:
    wanted = _norm(label)
    return [
        (row_index, column, str(cell))
        for row_index, row in enumerate(table)
        for column, cell in enumerate(row)
        if _norm(cell) == wanted
    ]


def _section_bounds(table: list[list[object]], series: str) -> tuple[int, int]:
    """Bound selectors to one numbered WSEI component, avoiding repeated dates."""
    section = _SERIES_SECTION[series]
    starts = [
        index for index, row in enumerate(table)
        if row and re.fullmatch(rf"\s*{section}\.\s*", str(row[0] or ""))
    ]
    if len(starts) != 1:
        raise ParseError(f"WSEI section {section} is missing or repeated")
    start = starts[0]
    end = len(table)
    for index in range(start + 1, len(table)):
        first = str(table[index][0] or "") if table[index] else ""
        if re.fullmatch(r"\s*\d+\.\s*", first):
            end = index
            break
    return start, end


def _select_observation(table: list[list[object]], *, selector: dict[str, Any]) -> ParseResult:
    """Select the row and one dated component column from an extracted table."""
    series = str(selector.get("series", ""))
    row_label = str(selector.get("row", ""))
    column_label = str(selector.get("column", ""))
    line_index = int(selector.get("line", 0))
    unit = str(selector.get("unit", ""))
    scale = float(selector.get("scale", 1))
    section_start, section_end = _section_bounds(table, series)
    section = table[section_start:section_end]
    unit_proved = _unit_proved(section, unit, scale)
    if unit == "percent" and series == "gdp_growth":
        # This WSEI table labels the measure as a growth rate but does not
        # repeat a percent sign in every cell. The named row is the source's
        # unit declaration; do not infer percent merely from the metric id.
        unit_proved = "gdp growth rate" in _norm(row_label)
    if not unit_proved:
        raise ParseError(f"WSEI component unit does not prove {unit!r}")
    if column_label == "latest_month":
        dated = [
            (row_index + section_start, column, str(cell))
            for row_index, row in enumerate(section)
            for column, cell in enumerate(row)
            if parse_wsei_period(str(cell or "")) is not None
            and re.fullmatch(
                r"\s*[A-Za-z]+,\s*(?:\d{4}|FY\s*\d{2})(?:[PR]\d*)?\s*",
                str(cell or ""), re.IGNORECASE,
            )
        ]
        # WSEI component 12 shares the date heading printed just before its
        # section marker. Accept that adjacent heading only for this known
        # table layout and only when the component contains no date labels.
        if not dated and series in {"credit", "exports"}:
            for row_index in range(section_start - 1, -1, -1):
                row = table[row_index]
                found = [
                    (row_index, column, str(cell))
                    for column, cell in enumerate(row)
                    if parse_wsei_period(str(cell or "")) is not None
                    and re.fullmatch(
                        r"\s*[A-Za-z]+,\s*(?:\d{4}|FY\s*\d{2})(?:[PR]\d*)?\s*",
                        str(cell or ""), re.IGNORECASE,
                    )
                ]
                if found:
                    dated = found
                    break
        if not dated:
            raise ParseError("WSEI section has no explicit calendar-month header")
        calendar_headers = [
            item for item in dated
            if re.fullmatch(r"\s*[A-Za-z]+,\s*\d{4}[PR]?\s*", item[2], re.IGNORECASE)
        ]
        if calendar_headers:
            dated = calendar_headers
        target = _norm(row_label)
        row_indices = [
            row_index
            for row_index in range(section_start, section_end)
            if any(target in _norm(line) for cell in table[row_index] for line in str(cell or "").splitlines())
        ]
        if not row_indices:
            row_indices = [
                row_index
                for row_index in range(section_end)
                if any(target in _norm(line) for cell in table[row_index] for line in str(cell or "").splitlines())
            ]
        if row_indices:
            nearest_header = max((item[0] for item in dated if item[0] <= min(row_indices)), default=None)
            if nearest_header is not None:
                dated = [item for item in dated if item[0] == nearest_header]
        newest = max(parse_wsei_period(item[2]) for item in dated)
        dated = [item for item in dated if parse_wsei_period(item[2]) == newest]
    elif column_label in {"latest_fy", "latest_complete_fy"}:
        dated = [
            (row_index + section_start, column, str(cell))
            for row_index, row in enumerate(section)
            for column, cell in enumerate(row)
            if re.fullmatch(r"FY\s*\d{2}[PR]?\d*", str(cell or "").strip(), re.IGNORECASE)
        ]
        if not dated and series == "exports":
            for row_index in range(section_start - 1, -1, -1):
                found = [
                    (row_index, column, str(cell))
                    for column, cell in enumerate(table[row_index])
                    if re.fullmatch(r"FY\s*\d{2}[PR]?\d*", str(cell or "").strip(), re.IGNORECASE)
                ]
                if found:
                    dated = found
                    break
        if not dated:
            raise ParseError("WSEI section has no fiscal-year column header")
        if column_label == "latest_complete_fy":
            # Current-year partial columns are explicitly month-ranged (e.g.
            # July-August FY27), so only bare FY labels are eligible here.
            dated = [item for item in dated if re.fullmatch(r"FY\s*\d{2}[PR]?\d*", item[2].strip(), re.IGNORECASE)]
        newest = max(parse_wsei_period(item[2]) for item in dated)
        dated = [item for item in dated if parse_wsei_period(item[2]) == newest]
    else:
        dated = [
            (row_index + section_start, column, header)
            for row_index, column, header in _header_matches(section, column_label)
        ]
    if not dated:
        raise ParseError(f"WSEI table is missing period header {column_label!r}")
    if len(dated) != 1:
        raise ParseError(f"ambiguous WSEI period header {column_label!r}")
    header_row, column, header = dated[0]
    period = parse_wsei_period(header)
    if period is None:
        raise ParseError(f"WSEI header has no recoverable period: {header!r}")

    subcolumn = selector.get("subcolumn")
    if subcolumn:
        date_header = table[header_row]
        group_end = len(date_header)
        for index in range(column + 1, len(date_header)):
            label = str(date_header[index] or "").strip()
            if label and (parse_wsei_period(label) is not None or "percentage change" in _norm(label)):
                group_end = index
                break
        matches = [
            candidate_column
            for row in table[section_start:min(section_end, header_row + 4)]
            for candidate_column in range(column, min(group_end, len(row)))
            if _norm(row[candidate_column]) == _norm(subcolumn)
        ]
        # A merged date header can label two subcolumns. Resolve the requested
        # subcolumn within only that dated group, never by a global ordinal.
        if len(matches) != 1:
            raise ParseError(f"ambiguous or missing WSEI subcolumn {subcolumn!r}")
        column = matches[0]

    row_matches: list[tuple[int, int]] = []
    target = _norm(row_label)
    for row_index in range(section_start, section_end):
        row = table[row_index]
        for label_column, cell in enumerate(row):
            lines = str(cell or "").splitlines()
            for item_index, item in enumerate(lines):
                if target == _norm(item) or target in _norm(item):
                    row_matches.append((row_index, item_index))
                    break
    if not row_matches:
        raise ParseError(f"WSEI row {row_label!r} not found")
    # Text labels may appear in an earlier numbered table heading. The actual
    # value row is the unique row whose selected column has a numeric cell at
    # the requested line index.
    value_matches: list[tuple[int, float]] = []
    for row_index, _ in row_matches:
        if row_index <= header_row:
            continue
        if column >= len(table[row_index]):
            continue
        try:
            raw = _line(table[row_index][column], line_index, role="value")
            value = _number(raw)
        except ParseError:
            continue
        value_matches.append((row_index, value))
    if len(value_matches) != 1:
        raise ParseError(f"ambiguous or missing WSEI value row {row_label!r}")
    value = value_matches[0][1] * scale
    status_context = " ".join(
        str(table[row_index][column] or "")
        for row_index in range(section_start, header_row + 1)
        if column < len(table[row_index])
    )
    status = _release_status(f"{header} {status_context}")
    return ParseResult(
        value=value, _parse_strategy="wsei_observation", source_as_of=period,
        unit=unit, release_status=status,
    )


def _instruction(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ParseError("WSEI observation instruction must be JSON") from exc
    required = {"series", "row", "column", "line", "unit"}
    if not isinstance(parsed, dict) or not required <= parsed.keys():
        raise ParseError(f"WSEI observation instruction requires {sorted(required)}")
    return parsed


@register("wsei_observation")
class WseiObservationParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        selector = _instruction(instruction)
        heading = _SERIES_HEADINGS.get(str(selector["series"]))
        if heading is None:
            raise ParseError(f"unknown WSEI series selector {selector['series']!r}")
        matches: list[ParseResult] = []
        with pdfplumber.open(artifact.artifact_path) as pdf:
            for page in pdf.pages:
                page_text = _norm(page.extract_text() or "")
                if heading not in page_text:
                    continue
                for table in page.extract_tables():
                    try:
                        matches.append(_select_observation(table, selector=selector))
                    except ParseError:
                        continue
        if not matches:
            raise ParseError(f"no WSEI table matched selector {selector!r}")
        if len(matches) != 1:
            raise ParseError(f"ambiguous WSEI table for selector {selector!r}")
        return matches[0]
