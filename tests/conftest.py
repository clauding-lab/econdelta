"""Shared pytest fixtures."""

import os
from pathlib import Path

import pytest

# Skip the Opus 4.6 review in aggregate_latest by default for all tests.
# Real Opus calls are slow ($$ + minutes), require subscription auth, and
# aren't what unit/integration tests are validating.
os.environ.setdefault("ECONDELTA_SKIP_OPUS_REVIEW", "1")

# Skip the Supabase metric_history upsert in aggregate_latest by default
# for tests. Tests targeting the writer mock requests.Session directly;
# end-to-end aggregate tests would otherwise need real Supabase creds.
os.environ.setdefault("ECONDELTA_SKIP_SUPABASE", "1")


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


@pytest.fixture(autouse=True)
def isolated_monthly_evidence(tmp_path, monkeypatch):
    """Tests must never leave synthetic source-check receipts in real data/."""
    monkeypatch.setattr("utils.monthly_evidence.DEFAULT_DIRECTORY", tmp_path / "monthly_evidence")


# Every file the aggregate (and the run wrapper) keeps in the git-ignored data/ between runs.
_AGGREGATE_STATE_PATHS: tuple[tuple[str, str], ...] = (
    ("aggregate_latest.DATA_DIR", ""),
    ("aggregate_latest.LATEST_PATH", "latest.json"),
    ("aggregate_latest.ARCHIVE_DIR", "archive"),
    ("aggregate_latest.STALENESS_STATE_PATH", "staleness_state.json"),
    ("aggregate_latest.WATCHLIST_STALENESS_STATE_PATH", "watchlist_staleness_state.json"),
    ("aggregate_latest.STALE_FALLBACK_ALERT_STATE_PATH", "stale_fallback_alert_state.json"),
    ("utils.supabase_writer._WRAP_RUN_CRASH_ALERT_STATE_PATH", "wrap_run_crash_alert_state.json"),
)


@pytest.fixture(autouse=True)
def isolated_aggregate_state(tmp_path_factory, monkeypatch):
    """Tests must never rewrite real data/ (archive/latest_<date>.json, staleness and alert state).

    A test that runs main() without redirecting every path used to overwrite the real archive and
    staleness files, and a stale alert de-dup file could silence a later test's alert. Each test
    gets its own empty data dir (outside its tmp_path, so tmp_path listings are unchanged); a test
    that sets one of these paths itself still wins, because its monkeypatch runs after this one.
    """
    data_dir = tmp_path_factory.mktemp("aggregate_data")
    for target, relative in _AGGREGATE_STATE_PATHS:
        monkeypatch.setattr(target, data_dir / relative if relative else data_dir)
