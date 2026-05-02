"""Shared pytest fixtures."""

import os
from pathlib import Path

import pytest

# Skip the Opus 4.6 review in aggregate_latest by default for all tests.
# Real Opus calls are slow ($$ + minutes), require subscription auth, and
# aren't what unit/integration tests are validating.
os.environ.setdefault("ECONDELTA_SKIP_OPUS_REVIEW", "1")


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to the tests/fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_html(fixtures_dir: Path):
    """Factory fixture: load an HTML fixture file by name.

    Usage:
        def test_foo(sample_html):
            html = sample_html("bb_forex_rates.html")
    """

    def _load(filename: str) -> str:
        path = fixtures_dir / filename
        return path.read_text(encoding="utf-8")

    return _load
