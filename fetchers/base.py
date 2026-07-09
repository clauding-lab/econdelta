"""Shared types for Stage 1 (fetch).

A FetchResult points at a cached artifact on disk. The artifact is
replayable — Stage 2 reads it without touching the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


class FetchError(RuntimeError):
    """Network failure, bot challenge, or discovery returned nothing."""


@dataclass(frozen=True)
class FetchResult:
    indicator_id: str
    artifact_path: Path
    artifact_type: Literal["pdf", "html"]
    fetched_at: datetime
    source_url: str
    sha256: str
    cache_hit: bool


def format_period(period: tuple[int, int]) -> str:
    """Canonical serialization of a discovered issue period → ``"YYYY-MM"``.

    Written into a PDF artifact's ``.meta.json`` sidecar at fetch time and read
    back by ``parse_all._load_artifact_for`` via :func:`parse_period` — the ONE
    place this on-disk format is defined, so read and write can never drift.
    """
    year, month = period
    return f"{year:04d}-{month:02d}"


def parse_period(value: object) -> tuple[int, int] | None:
    """Parse a sidecar ``period`` field back to ``(year, month)``.

    Tolerant of legacy/absent/malformed values — returns ``None`` so the caller
    falls back to mtime rather than raising on a hand-edited sidecar.
    """
    if not isinstance(value, str):
        return None
    parts = value.split("-")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None
