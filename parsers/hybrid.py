"""Hybrid orchestrator: deterministic-first with Sonnet 4.6 sanity-check + fallback."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_max.max_client import MaxCallError, run_max
from claude_max.validators import InvalidValueError, validate_value, values_match
from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import get_parser

logger = logging.getLogger("hybrid")
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "claude_max" / "prompts"

# Sonnet sees this many chars from the artifact. Old value was 6000 which
# truncated multi-page PDFs to TOC + first page of exec summary.
LLM_TEXT_CAP = 30000

_PAGE_HINT_RE = re.compile(r"pages?\s+(\d+)", re.IGNORECASE)


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


def _parse_page_hint(instruction: str) -> int | None:
    """Extract a 1-indexed page number from English like 'Go to page 15 of the doc'.

    Returns None when no `page N` / `pages N-M` token is present.
    """
    if not instruction:
        return None
    m = _PAGE_HINT_RE.search(instruction)
    return int(m.group(1)) if m else None


def _extract_pdf_text(
    pdf_path: Path,
    page_hint: int | None,
    *,
    window: int = 3,
    indicator_id: str = "",
) -> str:
    """Extract text from a PDF, optionally limited to a window around `page_hint`.

    `page_hint` is 1-indexed. When set, returns text from pages
    [page_hint - window .. page_hint + window], clamped to doc bounds.
    Default window=3 absorbs the typical 1-3 page cover/TOC offset between
    a PDF's printed page numbers and pdfplumber's 0-indexed positions.
    When `page_hint` is None, returns text for the whole doc.

    Emits a debug line when ECONDELTA_DEBUG_PDF=1 is set.
    """
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        if page_hint is not None:
            target = page_hint - 1
            start = max(0, target - window)
            end = min(total, target + window + 1)
            pages = pdf.pages[start:end]
        else:
            pages = pdf.pages
            start, end = 0, total
        text = "\n".join((p.extract_text() or "") for p in pages)

    if os.environ.get("ECONDELTA_DEBUG_PDF"):
        logger.info(
            "pdf_text indicator=%s len=%d pages=%d-%d/%d hint=%s first500=%r last500=%r",
            indicator_id or "?",
            len(text),
            start + 1,
            end,
            total,
            page_hint,
            text[:500],
            text[-500:],
        )
    return text


def _sanity_check(*, indicator: dict, value: float, history: list[float]) -> Any:
    template = _load_prompt("sanity_check.txt")
    prompt = template.format(
        indicator_name=indicator["name"],
        domain=indicator["domain"],
        cadence=indicator["cadence"],
        value=value,
        value_type=indicator["parse"]["value_type"],
        valid_range=indicator["parse"]["valid_range"],
        history=history or "(none)",
    )
    return run_max(prompt=prompt)


def _llm_extract(*, indicator: dict, artifact: FetchResult) -> Any:
    template = _load_prompt(indicator["parse"]["llm_prompt"])
    instruction = indicator["fetch"].get("task", "")
    if artifact.artifact_type == "pdf":
        page_hint = _parse_page_hint(instruction)
        text = _extract_pdf_text(
            artifact.artifact_path,
            page_hint=page_hint,
            indicator_id=indicator["id"],
        )
        prompt = template.format(
            indicator_name=indicator["name"],
            instruction=instruction,
            value_type=indicator["parse"]["value_type"],
            valid_range=indicator["parse"]["valid_range"],
            pdf_text=text[:LLM_TEXT_CAP],
        )
    else:
        text = artifact.artifact_path.read_text()
        prompt = template.format(
            indicator_name=indicator["name"],
            instruction=instruction,
            value_type=indicator["parse"]["value_type"],
            valid_range=indicator["parse"]["valid_range"],
            html_text=text[:LLM_TEXT_CAP],
        )
    return run_max(prompt=prompt)


def _build_snapshot(
    *, indicator: dict, artifact: FetchResult, value: Any,
    provenance: str, parse_strategy: str, sanity_note: str | None = None,
    previous_value: float | None = None, change_pct: float | None = None,
) -> dict:
    return {
        "indicator_id": indicator["id"],
        "name": indicator["name"],
        "domain": indicator["domain"],
        "cadence": indicator["cadence"],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source_url": artifact.source_url,
        "value": value,
        "value_type": indicator["parse"]["value_type"],
        "previous_value": previous_value,
        "change_pct": change_pct,
        "_provenance": provenance,
        "_artifact_sha256": artifact.sha256,
        "_parse_strategy": parse_strategy,
        "sanity_note": sanity_note,
    }


def parse_one(artifact: FetchResult, indicator: dict, history: list[float]) -> dict:
    parse_block = indicator["parse"]
    instruction = indicator["fetch"].get("task", "")
    value_type = parse_block["value_type"]
    valid_range = tuple(parse_block["valid_range"])

    parser = get_parser(parse_block["deterministic"])
    v_det: Any = None
    try:
        det_result: ParseResult = parser.parse(artifact, instruction)
        # value can be a dict (e.g. call_money) — only validate scalar values
        if isinstance(det_result.value, (int, float)):
            validate_value(value=det_result.value, value_type=value_type, valid_range=valid_range)
        v_det = det_result.value
    except (ParseError, InvalidValueError) as e:
        logger.info("deterministic parse failed for %s: %s", indicator["id"], e)

    if v_det is not None:
        # Sanity-check via Sonnet
        try:
            check_value = float(v_det) if isinstance(v_det, (int, float)) else 0.0
            sanity = _sanity_check(indicator=indicator, value=check_value, history=history)
            plausible = bool((sanity.parsed or {}).get("plausible", True))
            note = (sanity.parsed or {}).get("reason")
        except MaxCallError as e:
            logger.warning("sanity-check failed for %s: %s — emitting deterministic anyway", indicator["id"], e)
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="deterministic", parse_strategy=parse_block["deterministic"])

        if plausible:
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="deterministic", parse_strategy=parse_block["deterministic"],
                                   sanity_note=note)
        # Disagreement: cross-check with extract
        try:
            extract = _llm_extract(indicator=indicator, artifact=artifact)
            v_llm = (extract.parsed or {}).get("value")
            if v_llm is not None and isinstance(v_det, (int, float)) and isinstance(v_llm, (int, float)):
                if values_match(float(v_det), float(v_llm), value_type=value_type):
                    return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                           provenance="deterministic", parse_strategy=parse_block["deterministic"],
                                           sanity_note=f"sanity flagged but extract agreed; {note}")
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="needs_review", parse_strategy=parse_block["deterministic"],
                                   sanity_note=f"det={v_det} llm={v_llm} note={note}")
        except MaxCallError as e:
            logger.warning("llm_extract failed for %s: %s", indicator["id"], e)
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="needs_review", parse_strategy=parse_block["deterministic"],
                                   sanity_note=f"sanity flagged, extract errored: {e}")

    # LLM extract path (deterministic failed)
    try:
        extract = _llm_extract(indicator=indicator, artifact=artifact)
        v_llm = (extract.parsed or {}).get("value")
        if v_llm is None:
            raise MaxCallError(f"llm extract returned no value: {extract.raw_text[:200]}")
        if isinstance(v_llm, (int, float)):
            validate_value(value=float(v_llm), value_type=value_type, valid_range=valid_range)
        return _build_snapshot(indicator=indicator, artifact=artifact, value=v_llm,
                               provenance="llm_extracted", parse_strategy=parse_block["deterministic"])
    except (MaxCallError, InvalidValueError) as e:
        logger.error("extract_failed for %s: %s", indicator["id"], e)
        return _build_snapshot(indicator=indicator, artifact=artifact, value=0.0,
                               provenance="needs_review", parse_strategy="extract_failed",
                               sanity_note=str(e))
