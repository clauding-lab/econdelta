"""Parser for "Component <ID>" labeled values in BB Monthly Economic Indicators PDFs.

Extended to extract ``source_as_of`` from BB FSAR / QFSAR cover-page text. Two
phrasings are recognised:
  - "Quarter ending DD Month YYYY"  (e.g. "Quarter ending 30 September 2025")
  - "... as of end-Month YYYY"      (e.g. the QFSAR's "data and information
    available as of end-September 2025") → maps to that month's quarter-end.

``recover_source_as_of`` exposes the same date recovery to ``parsers/hybrid.py``
so the publication date survives even when value extraction falls through to the
LLM path (the QFSAR's exec-summary prose is not a "Component <ID>" label, so the
deterministic value parse raises and the LLM supplies the value instead).
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

logger = logging.getLogger(__name__)

# Matches "Quarter ending 30 September 2025" on the FSAR cover page.
# Group 1: day (1-31), Group 2: month name, Group 3: 4-digit year.
_QUARTER_END_RE = re.compile(
    r"quarter\s+ending\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    re.IGNORECASE,
)

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Matches the QFSAR's reference-date line, e.g. "available as of end-September
# 2025" or "as at end of December 2025". Group 1: month, Group 2: year.
#
# The leading "as of|at|on" prefix is REQUIRED — this is the report's idiom for
# stating its own reporting period, and anchoring on it rejects two false
# positives: comparison-quarter prose ("compared to end-June 2025") and
# hyphenated compounds ("front-end March 2026"). The "end" token then needs a
# real separator (``[\s\-]+``, not ``*``) so "endApril" can't slip through. A
# phrasing this misses simply yields None — caught by the slow-cadence guardrail
# in aggregate_latest, which is the safe failure (no wrong date is fabricated).
_END_MONTH_RE = re.compile(
    r"\bas\s+(?:of|at|on)\s+end[\s\-]+(?:of\s+)?(" + "|".join(_MONTH_NAMES) + r")\s+(\d{4})",
    re.IGNORECASE,
)


def _extract_quarter_end(text: str) -> date | None:
    """Return the quarter-end (reporting period-end) date from FSAR cover text,
    or None if no recognised phrasing is present."""
    # 1. Explicit "Quarter ending DD Month YYYY".
    m = _QUARTER_END_RE.search(text)
    if m:
        day = int(m.group(1))
        month = _MONTH_NAMES.get(m.group(2).lower())
        year = int(m.group(3))
        if month is None:
            return None
        try:
            return date(year, month, day)
        except ValueError:
            return None
    # 2. "... as of end-Month YYYY" → the last calendar day of that month.
    m = _END_MONTH_RE.search(text)
    if m:
        month = _MONTH_NAMES.get(m.group(1).lower())
        year = int(m.group(2))
        if month is not None:
            try:
                # monthrange(...)[1] = last calendar day of that month (handles
                # leap-year Feb). ValueError guards a malformed year (defensive;
                # the regex already constrains month to a known name).
                return date(year, month, calendar.monthrange(year, month)[1])
            except ValueError:
                return None
    return None


@register("pdf_component")
class PdfComponentParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        with pdfplumber.open(artifact.artifact_path) as pdf:
            full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        pattern = re.escape(instruction) + r"[^\d\-]*([\-]?[0-9][0-9,\.]*)"
        m = re.search(pattern, full_text, re.IGNORECASE)
        if not m:
            raise ParseError(f"component {instruction!r} not found in PDF")
        cleaned = m.group(1).replace(",", "")
        source_as_of = _extract_quarter_end(full_text)
        return ParseResult(
            value=float(cleaned),
            _parse_strategy="pdf_component",
            source_as_of=source_as_of,
        )

    def recover_source_as_of(self, artifact: FetchResult) -> date | None:
        """Recover the FSAR reporting period-end date from the cover, even when
        value extraction fails and the LLM path supplies the value.

        Scans only the first two pages: the report states its own reference date
        on the cover ("... available as of end-September 2025"), away from the
        comparison-quarter mentions deeper in the document. Best-effort — any
        read error yields None rather than breaking the parse.
        """
        try:
            with pdfplumber.open(artifact.artifact_path) as pdf:
                cover = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
        except Exception as exc:  # noqa: BLE001 — recovery must never be fatal
            logger.debug(
                "source_as_of recovery could not read PDF for %s: %s",
                artifact.indicator_id, exc,
            )
            return None
        return _extract_quarter_end(cover)
