"""BB Government Securities Online Market total-outstanding row, with its date.

Extraction is exactly ``html_table_row`` — same ``row=<label> col=<n>``
instruction, same number cleaning (the portal uses Bangladeshi lakh-crore
grouping, ``22,10,000.00`` = 2,210,000.00). This parser exists for the one
thing the generic parser cannot do: recover WHICH DAY the figure describes.

The gsom pages are date-parameterised — ``fetchers.dated_form`` posts a
``picker_date`` and the page answers for that day, echoing it back in the
input's ``value``. Since the fetcher walks backwards past empty days (the
T-bill table is blank at the 01:11 BDT fetch hour, on the Friday/Saturday
weekend, and on the odd ordinary weekday), the figure we publish is often
NOT today's. Reading the echoed date off the page and returning it as
``source_as_of`` is what keeps that honest: the value is dated by the day it
reports, and ``metric_history.as_of`` follows.

That distinction is not cosmetic here. An undated figure is exactly what let
a correct fiscal-year reset be read as a collapse (landmine 56) — a value
whose period nothing records cannot be reasoned about later.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.html_table_row import _parse_instruction, _to_number
from parsers.registry import register

# The portal renders the picker as an uppercase dd-MMM-yy ("03-SEP-26"); its
# own datepicker JS upper-cases whatever the user selects.
_PICKER_RE = re.compile(
    r"""<input[^>]*\bname=["']picker_date["'][^>]*\bvalue=["']([^"']*)["']""",
    re.IGNORECASE,
)
_PICKER_DATE_FORMAT = "%d-%b-%y"


def _picker_date(html: str) -> date | None:
    """The date the page answered for, or None if it isn't stated.

    Never raises: a missing or reshaped picker costs the figure its
    ``source_as_of``, which is a degradation, not a parse failure.
    """
    match = _PICKER_RE.search(html)
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    try:
        # "03-SEP-26" -> "03-Sep-26": %b matches the C-locale abbreviation.
        return datetime.strptime(raw.title(), _PICKER_DATE_FORMAT).date()
    except ValueError:
        return None


@register("gsom_total_row")
class GsomTotalRowParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        row_label, col = _parse_instruction(instruction)
        html = artifact.artifact_path.read_text()
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            first = cells[0].get_text(strip=True)
            if row_label.lower() in first.lower():
                if len(cells) < col:
                    raise ParseError(
                        f"row {row_label!r} has only {len(cells)} cells, need col {col}"
                    )
                return ParseResult(
                    value=_to_number(cells[col - 1].get_text(strip=True)),
                    _parse_strategy="gsom_total_row",
                    source_as_of=_picker_date(html),
                )
        raise ParseError(f"row matching {row_label!r} not found")

    def recover_source_as_of(self, artifact: FetchResult) -> date | None:
        """Date recovery for the LLM-extract fallback path.

        ``parsers.hybrid`` calls this when value extraction failed, so a
        figure the LLM rescues still carries the day it reports.
        """
        return _picker_date(artifact.artifact_path.read_text())
