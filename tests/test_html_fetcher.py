"""Integration test for fetch_html."""
from pathlib import Path

import pytest

from fetchers.html_fetcher import fetch_html


@pytest.fixture
def fixture_page(tmp_path: Path) -> str:
    p = tmp_path / "page.html"
    p.write_text("<html><body><table id='rates'><tr><td>USD/BDT</td><td>122.5</td></tr></table></body></html>")
    return p.as_uri()


def test_fetch_html_writes_snapshot_and_returns_result(fixture_page, tmp_path):
    snap_dir = tmp_path / "snaps"
    fr = fetch_html(url=fixture_page, indicator_id="usd_bdt_test", snapshot_dir=snap_dir)
    assert fr.indicator_id == "usd_bdt_test"
    assert fr.artifact_type == "html"
    assert fr.artifact_path.exists()
    assert "USD/BDT" in fr.artifact_path.read_text()
    assert len(fr.sha256) == 64
    assert fr.cache_hit is False


def test_fetch_html_detects_cache_hit_on_unchanged_content(fixture_page, tmp_path):
    snap_dir = tmp_path / "snaps"
    fetch_html(url=fixture_page, indicator_id="usd_bdt_test", snapshot_dir=snap_dir)
    fr2 = fetch_html(url=fixture_page, indicator_id="usd_bdt_test", snapshot_dir=snap_dir)
    assert fr2.cache_hit is True
