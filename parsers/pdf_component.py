"""Parser for "Component <ID>" labeled values in BB Monthly Economic Indicators PDFs.

Extended to extract ``source_as_of`` from the source document's own date idiom.
Two report families are recognised, gated by a CONTENT marker (never tried
unconditionally — see the false-positive history below):

  - BB "Major Economic Indicators: Monthly Update" — gated on the document's
    own title marker (``_MEI_TITLE_MARK``), then reads the "Monthly Update
    (Month YYYY)" / "Volume MM/YYYY Month YYYY" cover idiom (the same idiom
    ``pdf_table_row.py``'s ``_bb_report_date`` recognises).
  - BB FSAR / QFSAR — anything NOT gated into the MEI branch above falls to
    "Quarter ending DD Month YYYY" (e.g. "Quarter ending 30 September 2025"),
    then "... as of end-Month YYYY" (e.g. the QFSAR's "data and information
    available as of end-September 2025").

Two false positives, both found against REAL captured document text, drove
this shape:

1. The generic "as of end-Month YYYY" idiom alone can false-positive-match an
   UNRELATED table header elsewhere in a multi-topic document — the MEI PDF's
   page-5 liquidity table prints "As of end June 2025 / As of end May 2026P"
   as column headers, which this idiom would misread as the WHOLE document's
   date. Fix: only reachable when the MEI branch's own gate did NOT match.
2. A short/loose MEI marker (mirroring ``pdf_table_row.py``'s bare
   ``"major economic indicators" in text.lower()`` check) ALSO false-positive-
   gates real FSAR/QFSAR documents: ``tests/fixtures/fsr_fixture_text.txt`` (a
   live FSAR capture) contains the substring "major economic indicators" as a
   SOURCE CITATION ("Source: Major Economic Indicators, January 2026 issue,
   BB.") — not the FSAR's own title. Gating on that bare substring would route
   a real FSAR document into the MEI branch, return None (no MEI cover idiom
   present), and lose the correct FSAR date the QFSAR-idiom fallback would
   otherwise have found. ``pdf_table_row.py``'s gate gets away with the loose
   marker only because it's never fed FSAR text (a different parser handles
   that report family there) — the SAME looseness is unsafe here because
   ``pdf_component`` serves BOTH families. Fix: gate on the fuller title
   phrase ``_MEI_TITLE_MARK`` ("major economic indicators: monthly update"),
   verified present in the real MEI fixture and absent from the real FSAR
   fixture's citation.

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

# BB "Major Economic Indicators: Monthly Update" cover/header idiom — mirrors
# pdf_table_row.py's _BB_MONTHLY_RE / _BB_VOLUME_RE. Kept as a local copy
# (rather than importing the private helper cross-module) to avoid coupling
# two independently-registered parsers together.
_MEI_MONTHLY_RE = re.compile(r"Monthly Update\s*\(\s*([A-Za-z]+)\s+(\d{4})\s*\)", re.IGNORECASE)
_MEI_VOLUME_RE = re.compile(r"Volume\s+\d{1,2}/\d{4}\s+([A-Za-z]+)\s+(\d{4})", re.IGNORECASE)

# Content gate for the MEI branch — the FULL title phrase, not the bare
# "major economic indicators" substring (see module docstring point 2: that
# shorter marker false-positive-matches a source citation inside real FSAR
# text). Case-insensitive substring check against the whole document text.
_MEI_TITLE_MARK = "major economic indicators: monthly update"

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


def _mei_report_date(text: str) -> date | None:
    """BB "Monthly Update" idiom → the LATEST month named, mapped to month-end.

    Mirrors pdf_table_row._bb_report_date exactly (same idiom, same report
    family) — the latest match wins so a prior-edition mention elsewhere in
    the doc ("Revised since Monthly Update (December 2025)") can't win over
    the current cover date.
    """
    for rx in (_MEI_MONTHLY_RE, _MEI_VOLUME_RE):
        dates: list[date] = []
        for m in rx.finditer(text):
            # _MONTH_NAMES (this module) is keyed by FULL month name, unlike
            # pdf_table_row._MONTHS (3-letter keys) — don't truncate here.
            month = _MONTH_NAMES.get(m.group(1).lower())
            if month is None:
                continue
            try:
                dates.append(date(int(m.group(2)), month, calendar.monthrange(int(m.group(2)), month)[1]))
            except ValueError:
                continue
        if dates:
            return max(dates)
    return None


def _extract_quarter_end(text: str) -> date | None:
    """Return the reporting period-end date from the source document's own
    date idiom, or None if no recognised phrasing is present. A CONTENT gate
    picks the report family first — see the module docstring for why this is
    not just priority-ordering (unconditionally trying the MEI idiom first
    would still let its Volume-regex fire on some other document's
    coincidental text; gating on the real family avoids that class of bug
    entirely, not just the one instance already found)."""
    # 0. BB "Major Economic Indicators: Monthly Update" — gated on its own
    # title, so a document that ISN'T this report never reaches this idiom
    # (whatever it would return, even None, is final — no fallthrough to the
    # FSAR/QFSAR idioms below).
    if _MEI_TITLE_MARK in text.lower():
        return _mei_report_date(text)
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
        """Recover the reporting period-end date from the cover, even when
        value extraction fails and the LLM path supplies the value.

        Scans only the first two pages: every recognised report (BB MEI's
        "Monthly Update (Month YYYY)" cover, or the FSAR/QFSAR's "... available
        as of end-September 2025") states its own reference date on the cover,
        away from the comparison-quarter mentions deeper in the document.
        Best-effort — any read error yields None rather than breaking the parse.
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
