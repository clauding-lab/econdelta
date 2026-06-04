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

_PROMPT = """You extract Bangladesh banking/economy figures from one news article for a banking desk.

Indicators to look for (the press may use different words):
{names}

Return AT MOST ONE finding per indicator — the single banking-SECTOR-WIDE / overall
headline figure. STRICT rules:
- OVERALL ONLY. Return the whole-banking-sector / nationwide figure. Do NOT return
  per-bank, per-category, or per-segment numbers (e.g. "private banks' NPL",
  "specialised banks' CAR", a single bank's figure, a sub-total).
- CORRECT UNIT. A ratio indicator (NPL ratio, CAR, inflation, credit growth) is a
  PERCENTAGE (e.g. 32.26) — NEVER a Taka/crore AMOUNT. If the article gives only an
  amount (e.g. "Tk 5.89 lakh crore") for a ratio indicator and not the overall
  percentage, return NOTHING for that indicator.
- PERIOD. "period" MUST be the explicit reporting date the article states (ISO
  YYYY-MM-DD, the last day of the stated month/quarter). If no clear period is
  stated, set "period" to null. NEVER guess a period.
- "quote" is the exact sentence containing the figure.
- If the article does not state an overall figure for an indicator, omit it.

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
