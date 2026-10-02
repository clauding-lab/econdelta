"""Parser for DSE sector heat — sector-level average of per-scrip % change.

Source (since 2026-10-01): ``https://www.dse.com.bd/api/live/prices``.
DSE retired its PHP site (``dsebd.org/recent_market_information.php`` now
returns HTTP 410) for a Next.js app around 24-28 Sep 2026. The new site's own
"latest share price" page fills its table from this JSON endpoint:

    {"cols": ["code","ltp","ycp","open","high","low","close","volume","value",
              "trades","percent","category","board","sector","assetType"],
     "rows": [["BRACBANK", 65.7, 65.9, ..., -0.3034, "A", "PUBLIC", "Bank", "EQ"], ...],
     "session": {"isOpen": false, "phase": "closed",
                 "sessionDate": "2026-10-01", "date": "2026-10-02", ...}}

The registry fetches it with the shared Playwright HTML fetcher, so the saved
artifact is usually Chromium's JSON viewer page (the body text inside a
``<pre>``); a raw JSON body is accepted too.

This parser:
  1. Reads each scrip's ``percent`` via the column index named in ``cols``
     (no positional guessing). ``percent`` is ``null`` for untraded scrips and
     for the debt boards; those are skipped, never coerced to 0.
  2. Loads ``config/dse_sector_constituents.json`` for the 8-sector taxonomy.
  3. Simple-averages each sector's constituents present in today's data.
  4. Returns ``{"Banks": -1.4, "NBFI": -1.1, ...}`` as a single dict value
     (Phase 3.1 V5 fidelity output shape — unchanged).

Market date: ``source_as_of`` is ``session.sessionDate`` — the trading session
the percents belong to — and ONLY when ``session.isOpen`` is false and
``phase == "closed"``. Any other phase (pre-open, open, halted, post-close)
raises ParseError: those percents are intraday/provisional and must never be
recorded as a day's close. ``session.date`` (today's Dhaka calendar date) is
deliberately NOT used — on a Friday it is a non-trading day.

Sectors with zero constituents present are omitted rather than emitting NaN,
so consumers can use a missing key as the "no fresh data" signal.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

_TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "config" / "dse_sector_constituents.json"


def _load_payload(text: str) -> dict:
    """Decode the prices payload from raw JSON or Chromium's ``<pre>`` JSON view."""
    body = text.strip()
    if body.startswith("<"):
        soup = BeautifulSoup(body, "html.parser")
        pre = soup.find("pre")
        body = (pre.get_text() if pre is not None else soup.get_text()).strip()
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        raise ParseError(f"DSE sector heat: artifact is not the prices JSON ({e})") from e
    if not isinstance(payload, dict):
        raise ParseError("DSE sector heat: prices JSON is not an object")
    return payload


def _session_date(payload: dict) -> date:
    """Return the closed session's market date, or raise ParseError."""
    session = payload.get("session")
    if not isinstance(session, dict):
        raise ParseError("DSE sector heat: prices JSON has no session block")
    phase = session.get("phase")
    if session.get("isOpen") is not False or phase != "closed":
        raise ParseError(
            f"DSE sector heat: session not closed (isOpen={session.get('isOpen')!r}, "
            f"phase={phase!r}); refusing to record intraday % changes"
        )
    raw = session.get("sessionDate")
    if not isinstance(raw, str):
        raise ParseError("DSE sector heat: session.sessionDate missing")
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise ParseError(f"DSE sector heat: bad session.sessionDate {raw!r}") from e


def _parse_scrip_pcts(payload: dict) -> dict[str, float]:
    """Map scrip code -> daily % change from the ``cols``/``rows`` table."""
    cols = payload.get("cols")
    rows = payload.get("rows")
    if not isinstance(cols, list) or not isinstance(rows, list):
        raise ParseError("DSE sector heat: prices JSON lacks cols/rows")
    try:
        code_i = cols.index("code")
        pct_i = cols.index("percent")
    except ValueError as e:
        raise ParseError(f"DSE sector heat: prices cols changed: {cols!r}") from e

    pcts: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) <= max(code_i, pct_i):
            continue
        code, pct = row[code_i], row[pct_i]
        # bool is an int subclass — exclude it explicitly.
        if not isinstance(code, str) or isinstance(pct, bool):
            continue
        if not isinstance(pct, (int, float)):
            continue  # null = untraded / debt board: no % change today
        code = code.strip().upper()
        if code and code not in pcts:
            pcts[code] = float(pct)
    return pcts


def _load_taxonomy() -> dict[str, list[str]]:
    """Return ``{sector: [scrip_code, ...]}`` from the canonical taxonomy file."""
    if not _TAXONOMY_PATH.exists():
        raise ParseError(f"sector taxonomy missing at {_TAXONOMY_PATH}")
    raw = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    sectors = raw.get("sectors", {})
    return {
        sector: list(block.get("constituents", []))
        for sector, block in sectors.items()
        if isinstance(block, dict)
    }


def _aggregate_by_sector(
    pcts: dict[str, float],
    taxonomy: dict[str, list[str]],
) -> dict[str, float]:
    """Simple-average the constituent % changes per sector.

    Sectors with NO constituents present in `pcts` are dropped (the brief
    treats a missing key as 'sector data unavailable').
    """
    out: dict[str, float] = {}
    for sector, constituents in taxonomy.items():
        present = [pcts[c] for c in constituents if c in pcts]
        if not present:
            continue
        out[sector] = round(sum(present) / len(present), 2)
    return out


@register("dse_sector_heat")
class DseSectorHeatParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        text = artifact.artifact_path.read_text(encoding="utf-8", errors="replace")
        payload = _load_payload(text)
        as_of = _session_date(payload)
        pcts = _parse_scrip_pcts(payload)
        if not pcts:
            raise ParseError("DSE sector heat: no scrip % changes in prices JSON")

        taxonomy = _load_taxonomy()
        sector_heat = _aggregate_by_sector(pcts, taxonomy)
        if not sector_heat:
            raise ParseError(
                "DSE sector heat: no taxonomy constituents matched any scrip in the data"
            )

        return ParseResult(
            value=sector_heat, _parse_strategy="dse_sector_heat", source_as_of=as_of
        )

    def recover_source_as_of(self, artifact: FetchResult) -> date | None:
        """Session date for the LLM-fallback path; None unless the session closed."""
        text = artifact.artifact_path.read_text(encoding="utf-8", errors="replace")
        try:
            return _session_date(_load_payload(text))
        except ParseError:
            return None
