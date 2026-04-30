"""Hybrid orchestrator: deterministic-first with Sonnet 4.6 sanity-check + fallback."""
from __future__ import annotations

import logging
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


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


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
    if artifact.artifact_type == "pdf":
        import pdfplumber
        with pdfplumber.open(artifact.artifact_path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        prompt = template.format(
            indicator_name=indicator["name"],
            instruction=indicator["fetch"].get("task", ""),
            value_type=indicator["parse"]["value_type"],
            valid_range=indicator["parse"]["valid_range"],
            pdf_text=text[:6000],
        )
    else:
        text = artifact.artifact_path.read_text()
        prompt = template.format(
            indicator_name=indicator["name"],
            instruction=indicator["fetch"].get("task", ""),
            value_type=indicator["parse"]["value_type"],
            valid_range=indicator["parse"]["valid_range"],
            html_text=text[:6000],
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
