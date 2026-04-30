"""Shared types for Stage 2 (parse).

ParseResult carries the extracted value plus provenance metadata that
flows into the final per-indicator snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
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
