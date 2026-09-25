"""Shared types for Stage 2 (parse).

ParseResult carries the extracted value plus provenance metadata that
flows into the final per-indicator snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

Provenance = Literal["deterministic", "llm_extracted", "llm_corrected", "needs_review"]


class ParseError(RuntimeError):
    """Deterministic parser couldn't extract a value (caught -> LLM fallback)."""


@dataclass(frozen=True)
class ParseResult:
    value: float | int | str | dict
    _provenance: Provenance = "deterministic"
    _parse_strategy: str = ""
    sanity_note: str | None = None
    source_as_of: date | None = None
    unit: str | None = None
    release_status: Literal["final", "provisional", "unknown"] = "unknown"
    """Actual economic observation date or period end, when recoverable.

    This travels with the selected value into daily history. A report's cover,
    download date, or article byline is not a substitute for its table's period.
    None means an unknown observation period: aggregate retains explicit
    unavailable metadata and omits dated history. Only the policy corridor has
    an explicit writer-confirmation convention; that is never a decision date.
    """
