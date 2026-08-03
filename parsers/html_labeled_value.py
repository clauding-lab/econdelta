"""Parser for label/value pairs laid out as divs rather than a real table.

Instruction syntax: ``panel=<css selector> label=<label text>``. ``panel`` is a
whitespace-free CSS selector scoping the search; ``label`` is everything after
``label=`` and MAY contain spaces (so it must come last).

Written for Bangladesh Bank's homepage POLICY RATES panel, which is a div
pseudo-table and therefore invisible to ``html_table_row`` (that parser only
walks ``<tr>``/``<td>``)::

    <div class="policy">
      <div class="display_table">
        <div><div>Policy Rate (Repo Rate)</div><div>9.50%</div></div>
        ...

Two deliberate design choices, both consequences of the bug this parser exists
to fix (the repo rate sat at a stale 10.00% for days after BB cut to 9.50%):

1. **The label match is exact after normalisation** (case-folded, whitespace
   collapsed, trailing colon dropped) — not a substring match. If BB renames a
   row, this raises ParseError and lists the labels it did find, so the run
   fails loudly instead of silently locking onto the wrong row. A silent wrong
   number is the failure mode we are engineering against; a noisy miss is fine.
2. **No ``recover_source_as_of``.** The panel carries a ``Last update:`` stamp,
   but it is not maintained: on 2026-08-03 it read ``15.02.2026`` while the
   values themselves already reflected the 2026-07-30 MPC decision — and the
   exchange-rate panel *directly beside it on the same page* was stamped that
   same day, so the staleness is this panel's, not the site's. Threading
   that stamp into ``as_of`` would date a current value five months stale and
   trip the freshness sentinel. Better no date than a wrong one.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

_INSTRUCTION_RE = re.compile(r"^\s*panel=(?P<panel>\S+)\s+label=(?P<label>.+?)\s*$")

# A value cell must be a bare number, optionally signed / thousands-separated /
# percent-suffixed. Anything else (a range, a footnote, two numbers) is a
# structure change we would rather hear about than guess at.
_VALUE_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?\s*%?$")


def _parse_instruction(instruction: str) -> tuple[str, str]:
    m = _INSTRUCTION_RE.match(instruction)
    if not m:
        raise ParseError(
            f"instruction must be 'panel=<css> label=<text>', got {instruction!r}"
        )
    return m.group("panel"), m.group("label")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(":").strip().casefold()


def _to_number(text: str) -> float:
    cleaned = text.strip()
    if not _VALUE_RE.match(cleaned):
        raise ParseError(f"value cell {text!r} is not a bare number")
    return float(cleaned.rstrip("%").strip().replace(",", ""))


@register("html_labeled_value")
class HtmlLabeledValueParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        panel_selector, label = _parse_instruction(instruction)
        soup = BeautifulSoup(artifact.artifact_path.read_text(), "html.parser")

        panel = soup.select_one(panel_selector)
        if panel is None:
            raise ParseError(f"panel {panel_selector!r} not found in artifact")

        want = _norm(label)
        seen: list[str] = []
        for node in panel.find_all(True):
            if node.find(True) is not None:
                continue  # only leaf elements carry a label
            text = node.get_text()
            if not text.strip():
                continue
            seen.append(_norm(text))
            if _norm(text) != want:
                continue
            sibling = node.find_next_sibling()
            while sibling is not None and not sibling.get_text().strip():
                sibling = sibling.find_next_sibling()
            if sibling is None:
                raise ParseError(f"label {label!r} has no value element beside it")
            return ParseResult(
                value=_to_number(sibling.get_text()),
                _parse_strategy="html_labeled_value",
            )

        raise ParseError(f"label {label!r} not found in {panel_selector!r}; saw {seen}")
