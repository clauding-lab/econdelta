"""Hybrid orchestrator: deterministic-first with Sonnet 4.6 sanity-check + fallback."""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
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
# HTML pages from BB.org.bd vary widely (some have heavy inline CSS, some
# embed table data inside <script> JSON). Strip only definitely-noise blocks
# (style, noscript) and raise the cap so multi-table pages fit whole.
# DO NOT strip <script> — BB injects data into inline scripts that the page's
# JS later renders into visible tables. Stripping scripts caused regressions
# in bill_bond_rates, policy_rate_slf_sdf, and interbank_repo_data.
LLM_HTML_CAP = 90000

_PAGE_HINT_RE = re.compile(r"pages?\s+(\d+)", re.IGNORECASE)
# Block-level noise tags whose contents are never useful to Sonnet.
_HTML_NOISE_TAGS = ("style", "noscript")
_NOISE_RE = re.compile(
    r"<(" + "|".join(_HTML_NOISE_TAGS) + r")\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
# OCR fallback fires when pdfplumber's text extraction returns less than this.
# 200 chars catches scanned PDFs (text=0) and minimal-text PDFs that won't help Sonnet.
_OCR_THRESHOLD_CHARS = 200


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


def _should_ocr(text: str) -> bool:
    """Decide whether to fall back to OCR after pdfplumber text extraction.

    True when the extracted text is empty or below `_OCR_THRESHOLD_CHARS`
    (typical signature of a scanned-image PDF with no text layer).
    """
    return len(text.strip()) < _OCR_THRESHOLD_CHARS


def _ocr_pdf_pages(
    pdf_path: Path,
    page_indices: list[int],
    *,
    indicator_id: str = "",
) -> str:
    """Run OCR over the given 0-indexed pages of `pdf_path` and return text.

    Requires `pytesseract` + `pdf2image` Python libs and `tesseract` +
    `poppler-utils` system binaries. Raises ImportError with a clear
    install hint when missing — never silently returns empty.
    """
    try:
        import pytesseract  # type: ignore[import-not-found]
        from pdf2image import convert_from_path  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "OCR fallback requires pytesseract + pdf2image (pip) and "
            "tesseract-ocr + poppler-utils (apt). Install or set "
            "ECONDELTA_DISABLE_OCR=1 to skip."
        ) from e

    if not page_indices:
        return ""
    # convert_from_path uses 1-indexed pages.
    first = min(page_indices) + 1
    last = max(page_indices) + 1
    images = convert_from_path(str(pdf_path), first_page=first, last_page=last, dpi=200)
    chunks = [pytesseract.image_to_string(img) for img in images]
    text = "\n".join(chunks)
    logger.info(
        "ocr_fallback indicator=%s pages=%d-%d ocr_len=%d",
        indicator_id or "?", first, last, len(text),
    )
    return text


def _clean_html(text: str) -> str:
    """Strip <script>, <style>, <noscript> blocks from raw HTML.

    Reduces token bloat so the data table fits within LLM_HTML_CAP. Does not
    parse the DOM — regex is sufficient for BB.org.bd's static markup and
    avoids the bs4 dependency in the hot path.
    """
    return _NOISE_RE.sub("", text)


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

    if _should_ocr(text) and not os.environ.get("ECONDELTA_DISABLE_OCR"):
        page_indices = list(range(start, end))
        text = _ocr_pdf_pages(pdf_path, page_indices, indicator_id=indicator_id)

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
        raw = artifact.artifact_path.read_text()
        text = _clean_html(raw)
        prompt = template.format(
            indicator_name=indicator["name"],
            instruction=instruction,
            value_type=indicator["parse"]["value_type"],
            valid_range=indicator["parse"]["valid_range"],
            html_text=text[:LLM_HTML_CAP],
        )
    return run_max(prompt=prompt)


def _build_snapshot(
    *, indicator: dict, artifact: FetchResult, value: Any,
    provenance: str, parse_strategy: str, sanity_note: str | None = None,
    previous_value: float | None = None, change_pct: float | None = None,
    source_as_of: "date | None" = None,
) -> dict:
    snapshot: dict = {
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
    if source_as_of is not None:
        snapshot["source_as_of"] = source_as_of.isoformat()
    return snapshot


def _recover_source_as_of(parser: Any, artifact: FetchResult) -> "date | None":
    """Best-effort publication-date recovery for the LLM-extract fallback path.

    The deterministic parser raised before it could supply ``source_as_of`` (its
    value-extraction failed), so ask the parser to recover the date straight from
    the artifact if it knows how. Parsers that don't implement
    ``recover_source_as_of`` simply yield None. Never raises — date recovery must
    not break value parsing.
    """
    recover = getattr(parser, "recover_source_as_of", None)
    if recover is None:
        return None
    try:
        return recover(artifact)
    except Exception as e:  # noqa: BLE001 — recovery is best-effort
        logger.warning("source_as_of recovery failed for %s: %s", artifact.indicator_id, e)
        return None


def _terminal_fallback(
    *, indicator: dict, artifact: FetchResult, error: str, last_good: dict | None,
) -> dict:
    """Build the snapshot for a parse that failed on every available path
    (deterministic, and — where configured — the LLM extract fallback).

    For `amount_*` value types (money), publishing a synthesised 0.0 is
    worse than publishing nothing new: 0.0 is a plausible-looking WRONG
    number for a balance metric, unlike an obviously-broken one
    (cab-memo-2026-08-05.md). When a last-good snapshot is available, hold
    it forward instead — `_provenance="stale_fallback"` reuses the exact tag
    aggregate_latest.py's own last-good mechanism already uses, so freshness
    reporting downstream (`_is_fresh(...) and _provenance != "stale_fallback"`)
    treats a held value as NOT fresh, same as that mechanism does. The held
    snapshot keeps its ORIGINAL `scraped_at`/`source_url`/etc. — it is
    yesterday's real reading, not stamped as if freshly parsed today.

    Non-money value types, and any money metric with no last-good on record
    (e.g. first-ever run), keep the original 0.0/needs_review behaviour —
    there is nothing safe to hold forward.
    """
    value_type = indicator["parse"]["value_type"]
    if value_type in _HOLD_LAST_GOOD_VALUE_TYPES and last_good is not None:
        held = dict(last_good)
        held["_provenance"] = "stale_fallback"
        held["sanity_note"] = (
            f"parse failed today ({error}); holding last-good value "
            f"from {held.get('_stale_from', '?')}"
        )
        return held
    return _build_snapshot(indicator=indicator, artifact=artifact, value=0.0,
                           provenance="needs_review", parse_strategy="extract_failed",
                           sanity_note=error)


_HOLD_LAST_GOOD_VALUE_TYPES = frozenset({
    "amount_bdt_crore", "amount_bdt_mn", "amount_usd_bn", "amount_usd_mn",
})


def parse_one(
    artifact: FetchResult, indicator: dict, history: list[float],
    last_good: dict | None = None,
) -> dict:
    parse_block = indicator["parse"]
    instruction = indicator["fetch"].get("task", "")
    value_type = parse_block["value_type"]
    valid_range = tuple(parse_block["valid_range"])
    has_llm_fallback = "llm_prompt" in parse_block

    parser = get_parser(parse_block["deterministic"])
    v_det: Any = None
    det_source_as_of = None  # publication date recovered by the deterministic parser
    try:
        det_result: ParseResult = parser.parse(artifact, instruction)
        # value can be a dict (e.g. call_money) — only validate scalar values
        if isinstance(det_result.value, (int, float)):
            validate_value(value=det_result.value, value_type=value_type, valid_range=valid_range)
        v_det = det_result.value
        det_source_as_of = det_result.source_as_of
    except (ParseError, InvalidValueError, ValueError) as e:
        # ValueError: a deterministic parser's own number-cleaning helper
        # (e.g. _to_number) can raise bare ValueError on unparseable residue
        # that doesn't hit its ParseError branch. Catching it here keeps
        # every parse failure on the ladder's designed fallback path instead
        # of escaping parse_one and silently dropping the day's snapshot.
        logger.info("deterministic parse failed for %s: %s", indicator["id"], e)

    if v_det is not None:
        # Dict-shaped values (e.g. dse_sector_heat: {sector: pct}) skip the
        # scalar Sonnet sanity check — the per-entry valid_range applied at
        # parse time is the structural guard. Emitting deterministic.
        if isinstance(v_det, dict):
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="deterministic", parse_strategy=parse_block["deterministic"],
                                   source_as_of=det_source_as_of)
        # Sanity-check via Sonnet (scalar values only)
        try:
            check_value = float(v_det)
            sanity = _sanity_check(indicator=indicator, value=check_value, history=history)
            plausible = bool((sanity.parsed or {}).get("plausible", True))
            note = (sanity.parsed or {}).get("reason")
        except MaxCallError as e:
            logger.warning("sanity-check failed for %s: %s — emitting deterministic anyway", indicator["id"], e)
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="deterministic", parse_strategy=parse_block["deterministic"],
                                   source_as_of=det_source_as_of)

        if plausible:
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="deterministic", parse_strategy=parse_block["deterministic"],
                                   sanity_note=note, source_as_of=det_source_as_of)
        if not has_llm_fallback:
            # No LLM configured for this indicator (e.g. current_account_balance,
            # where the extraction prompt was itself the source of a two-month
            # alternation bug — cab-memo-2026-08-05.md) — there is nothing to
            # cross-check against. Publish the deterministic value (it already
            # passed validate_value) but flag for human review rather than
            # silently trusting a sanity-check disagreement.
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="needs_review", parse_strategy=parse_block["deterministic"],
                                   sanity_note=f"sanity flagged, no llm_prompt configured for cross-check: {note}",
                                   source_as_of=det_source_as_of)
        # Disagreement: cross-check with extract
        try:
            extract = _llm_extract(indicator=indicator, artifact=artifact)
            v_llm = (extract.parsed or {}).get("value")
            if v_llm is not None and isinstance(v_det, (int, float)) and isinstance(v_llm, (int, float)):
                if values_match(float(v_det), float(v_llm), value_type=value_type):
                    return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                           provenance="deterministic", parse_strategy=parse_block["deterministic"],
                                           sanity_note=f"sanity flagged but extract agreed; {note}",
                                           source_as_of=det_source_as_of)
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="needs_review", parse_strategy=parse_block["deterministic"],
                                   sanity_note=f"det={v_det} llm={v_llm} note={note}",
                                   source_as_of=det_source_as_of)
        except MaxCallError as e:
            logger.warning("llm_extract failed for %s: %s", indicator["id"], e)
            return _build_snapshot(indicator=indicator, artifact=artifact, value=v_det,
                                   provenance="needs_review", parse_strategy=parse_block["deterministic"],
                                   sanity_note=f"sanity flagged, extract errored: {e}",
                                   source_as_of=det_source_as_of)

    # Deterministic parse failed outright. If no LLM is configured for this
    # indicator, there is no extraction fallback left to try — go straight to
    # the terminal fallback (hold-last-good for money metrics) rather than
    # raising KeyError on the missing `llm_prompt` config key.
    if not has_llm_fallback:
        logger.info(
            "deterministic parse failed for %s and no llm_prompt is configured "
            "— using terminal fallback", indicator["id"],
        )
        return _terminal_fallback(
            indicator=indicator, artifact=artifact,
            error="deterministic parse failed; no llm_prompt configured for this indicator",
            last_good=last_good,
        )

    # LLM extract path (deterministic failed). Recover the publication date
    # directly from the artifact so a slow-cadence metric (e.g. the quarterly
    # FSAR) is dated by its real reporting period, not stamped with today's run
    # date — the latter made a stale Q3-2025 NPL read as fresh on The Brief.
    try:
        extract = _llm_extract(indicator=indicator, artifact=artifact)
        v_llm = (extract.parsed or {}).get("value")
        if v_llm is None:
            raise MaxCallError(f"llm extract returned no value: {extract.raw_text[:200]}")
        # Reject bool before the isinstance(..., (int, float)) check below —
        # bool is an int subclass, and float(True) == 1.0 would silently
        # strip the type info that validate_value's own bool guard checks,
        # letting a boolean sneak into the snapshot as a valid number.
        if isinstance(v_llm, bool) or not isinstance(v_llm, (int, float)):
            raise InvalidValueError(f"llm extract returned non-numeric value: {v_llm!r}")
        validate_value(value=float(v_llm), value_type=value_type, valid_range=valid_range)
        return _build_snapshot(indicator=indicator, artifact=artifact, value=v_llm,
                               provenance="llm_extracted", parse_strategy=parse_block["deterministic"],
                               source_as_of=_recover_source_as_of(parser, artifact))
    except (MaxCallError, InvalidValueError) as e:
        logger.error("extract_failed for %s: %s", indicator["id"], e)
        return _terminal_fallback(indicator=indicator, artifact=artifact, error=str(e), last_good=last_good)
