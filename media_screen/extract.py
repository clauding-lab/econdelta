"""Extract (indicator, value, period, quote) triples from press article text.

Uses the Max CLI. Best-effort: any LLM/parse error yields [] so the screen
never breaks (spec §9). The prompt forces an explicit period or null — the
downstream strict filter discards null-period findings.
"""
from __future__ import annotations

import logging
from datetime import date

from claude_max.max_client import MaxCallError, run_max
from media_screen.types import Extracted, MetricSpec

logger = logging.getLogger("media_extract")

_PROMPT = """You extract Bangladesh-economy figures from a news article for a banking desk.

For EACH of these indicators, if the article states a number for it, return one finding:
{names}

Rules:
- "period" MUST be the explicit reporting date the article gives (ISO YYYY-MM-DD,
  using the last day of the stated month/quarter). If the article does not state a
  clear period for the number, set "period" to null. NEVER guess a period.
- "value" is the bare number (percent as a number, e.g. 32.26).
- "quote" is the exact sentence containing the number.
Return JSON ONLY: {{"findings": [{{"press_name": "...", "value": 0.0, "period": "YYYY-MM-DD"|null, "quote": "..."}}]}}

ARTICLE:
{text}
"""


def _parse_period(raw) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def extract_numbers(
    text: str, *, specs: list[MetricSpec], source_url: str, source_outlet: str,
) -> list[Extracted]:
    names = "\n".join(f"- {n}" for s in specs for n in s.press_names)
    prompt = _PROMPT.format(names=names, text=text[:20000])
    try:
        result = run_max(prompt=prompt, effort="high")
    except MaxCallError as e:
        logger.warning("media extract LLM failed for %s: %s", source_url, e)
        return []
    findings = (result.parsed if isinstance(result.parsed, dict) else {}).get("findings") or []
    out: list[Extracted] = []
    for f in findings:
        try:
            value = float(f["value"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(Extracted(
            indicator_hint=str(f.get("press_name", "")),
            value=value,
            period=_parse_period(f.get("period")),
            quote=str(f.get("quote", "")),
            source_url=source_url,
            source_outlet=source_outlet,
        ))
    return out
