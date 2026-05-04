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
