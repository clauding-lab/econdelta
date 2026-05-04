"""Shared types for Stage 2 (parse).

ParseResult carries the extracted value plus provenance metadata that
flows into the final per-indicator snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    """Publication date of the source document, when recoverable.

    When a parser can determine *when* the source data was published (e.g.
    the quarter-end date on a BB FSAR PDF cover, or the article byline date
    on a news page), it sets this field. The aggregate pipeline threads it
    into ``metric_history.as_of`` so the Brief's freshness pill reflects
    the true publication date rather than the EconDelta run date.

    None means "unrecoverable — fall back to write date in the writer".
    """
