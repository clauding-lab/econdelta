"""Row-label parser for the MoF Monthly Fiscal Report (MFR) revenue tables.

Why this is NOT just `pdf_table_row`: the MFR column grids are not
fixed-position and `pdfplumber.extract_tables()` mangles them (numbers collapse
into a single cell), which is the documented reason `scripts/mfr_parser.py`
exists for the monthly-backfill namespace. This parser brings the SAME
row-anchored `extract_text()` technique into the DAILY pipeline registry so a
daily metric (e.g. Non-NBR-tax revenue, MFR Table 4 row "b. Non-NBR Tax") can
be read deterministically instead of relying on the LLM fallback every run.

Instruction grammar (machine tokens, whitespace-separated):

    row=<label> [anchor=<fy_budget_crore>] [col=month|fytd]

  - ``row``    : substring of the table row label (whitespace-insensitive,
                 case-insensitive). e.g. ``row=Non-NBR``.
  - ``anchor`` : the current fiscal-year ANNUAL BUDGET figure for that row.
                 The MFR repeats this stable value every monthly issue, so it
                 is a layout-independent anchor: the single-month figure is the
                 number immediately AFTER it and the FYTD figure is the one
                 after that (mirrors ``scripts/mfr_parser._extract_month_fytd``).
                 If omitted, the parser cannot disambiguate columns and raises
                 ParseError (-> LLM fallback).
  - ``col``    : which figure to return — ``fytd`` (default, the YTD figure the
                 revenue donut wants) or ``month`` (the stand-alone month).

Raises ``ParseError`` on any failure so the hybrid orchestrator falls through
to the LLM extract (never returns None).
"""
from __future__ import annotations

import re

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# Numeric token: optional leading minus, digits with optional thousands commas,
# optional decimal. Matches "-7,521", "104,000", "3.54", "0".
_NUM = re.compile(r"-?\d[\d,]*\.?\d*")
# A line made up only of numbers + whitespace (wrapped-label rows park their
# values on such a standalone line — the August-2025 MFR layout quirk).
_NUMERIC_LINE_RE = re.compile(r"^[\s\d,.\-]+$")


def _parse_instruction(instruction: str) -> dict:
    out: dict[str, str] = {}
    for token in instruction.split():
        if "=" in token:
            k, v = token.split("=", 1)
            out[k] = v
    if "row" not in out:
        raise ParseError(f"instruction missing row=<label>: {instruction!r}")
    col = out.get("col", "fytd")
    if col not in ("fytd", "month"):
        raise ParseError(f"col must be 'fytd' or 'month', got {col!r}")
    out["col"] = col
    return out


def _to_float(token: str) -> float:
    return float(token.replace(",", ""))


def _tokens_of(line: str) -> list[str]:
    return [t for t in _NUM.findall(line) if re.search(r"\d", t)]


def _numbers_after_label(text: str, label: str, *, lookahead: int = 3) -> list[str]:
    """Return the numeric tokens for the row anchored by ``label``.

    Handles the same three observed MFR layouts as
    ``scripts/mfr_parser._numbers_after_label``: numbers on the label line,
    a label-prefix match, and a wrapped label whose values sit on a standalone
    numeric line within the next ``lookahead`` lines.
    """
    label_norm = re.sub(r"\s+", "", label.lower())
    lines = text.split("\n")
    for li, line in enumerate(lines):
        line_norm = re.sub(r"\s+", "", line.lower())
        if label_norm not in line_norm:
            continue
        # Numbers on the same line, after the label text.
        collapsed = ""
        for i, ch in enumerate(line):
            if not ch.isspace():
                collapsed += ch.lower()
            if collapsed.endswith(label_norm):
                same_line = _tokens_of(line[i + 1:])
                if same_line:
                    return same_line
                break
        # Wrapped label — scan the next few lines for the first numeric line.
        for nxt in lines[li + 1: li + 1 + lookahead]:
            if _NUMERIC_LINE_RE.match(nxt) and _tokens_of(nxt):
                return _tokens_of(nxt)
    raise ParseError(f"row {label!r} not found (or no numeric row) in MFR text")


def _extract_after_anchor(numbers: list[float], anchor: float, offset: int) -> float:
    """Return numbers[anchor_idx + offset] where anchor_idx is the LAST cell
    matching ``anchor`` (the current-FY block trails the prior-FY block, whose
    budget can coincide). ``offset`` is 1 for the month figure, 2 for FYTD.
    """
    anchor_idx = None
    for i, v in enumerate(numbers):
        if abs(v - anchor) < 0.5:
            anchor_idx = i
    if anchor_idx is None:
        raise ParseError(f"FY budget anchor {anchor} not found in row numbers {numbers}")
    if anchor_idx + offset >= len(numbers):
        raise ParseError(
            f"anchor at index {anchor_idx} has no value at offset {offset} in {numbers}"
        )
    return numbers[anchor_idx + offset]


@register("pdf_mfr_row")
class PdfMfrRowParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        import pdfplumber

        ins = _parse_instruction(instruction)
        row_label = ins["row"]
        if "anchor" not in ins:
            raise ParseError(
                f"instruction missing anchor=<fy_budget> needed to locate the "
                f"{ins['col']} column for row {row_label!r}: {instruction!r}"
            )
        try:
            anchor = _to_float(ins["anchor"])
        except ValueError as e:
            raise ParseError(f"anchor not numeric: {ins['anchor']!r}") from e
        offset = 2 if ins["col"] == "fytd" else 1

        with pdfplumber.open(artifact.artifact_path) as pdf:
            page_text = None
            for page in pdf.pages:
                txt = page.extract_text() or ""
                if re.sub(r"\s+", "", row_label.lower()) in re.sub(r"\s+", "", txt.lower()):
                    page_text = txt
                    break
        if page_text is None:
            raise ParseError(f"no page contains row label {row_label!r}")

        tokens = _numbers_after_label(page_text, row_label)
        numbers = [_to_float(t) for t in tokens]
        value = _extract_after_anchor(numbers, anchor, offset)
        return ParseResult(value=value, _parse_strategy="pdf_mfr_row")
