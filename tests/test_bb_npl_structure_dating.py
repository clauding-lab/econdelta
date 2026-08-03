"""tests/test_bb_npl_structure_dating.py"""
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_TEXT = (REPO_ROOT / "tests" / "fixtures" / "fsr_fixture_text.txt").read_text()


def test_derive_position_date_from_real_fixture_is_end_dec_2025():
    from scrapers.bb_npl_structure import derive_position_date
    assert derive_position_date(FIXTURE_TEXT) == date(2025, 12, 31)


def test_latest_idiom_wins_and_end_of_variant_parses():
    from scrapers.bb_npl_structure import derive_position_date
    text = "as at end-December 2024 ... at the end of December 2025 the sector"
    assert derive_position_date(text) == date(2025, 12, 31)


def test_no_recognizable_date_raises():
    from scrapers.bb_npl_structure import PositionDateError, derive_position_date
    with pytest.raises(PositionDateError):
        derive_position_date("no dates here")


def test_slice_table_window_contains_table_and_total_row():
    from scrapers.bb_npl_structure import slice_table_window
    window = slice_table_window(FIXTURE_TEXT)
    assert "SECTOR-WISE NON-PERFORMING LOANS DISTRIBUTION" in window
    assert "Trade and Commerce" in window
    assert "18,204.30" in window          # the table's own total row
    assert len(window) < 20_000            # a slice, not the whole document


def test_slice_uses_last_marker_not_toc():
    from scrapers.bb_npl_structure import slice_table_window
    window = slice_table_window(FIXTURE_TEXT)
    assert "705.90" in window              # Agriculture data row, only near the real table


def test_slice_missing_marker_raises():
    from scrapers.bb_npl_structure import TableMarkerError, slice_table_window
    with pytest.raises(TableMarkerError):
        slice_table_window("an FSR whose layout changed completely")


def test_fetch_latest_fsr_wires_discovery_to_fetch(tmp_path):
    import scrapers.bb_npl_structure as mod
    fr = MagicMock(artifact_path=tmp_path / "fsr.pdf")
    with patch.object(mod, "_download_index_html", return_value="<html>") as dl, \
         patch.object(mod, "discover_latest_pdf", return_value=("https://x/f.pdf", (2026, 6))) as disc, \
         patch.object(mod, "fetch_pdf", return_value=fr) as fp:
        out = mod.fetch_latest_fsr(tmp_path)
    assert out is fr
    dl.assert_called_once_with(mod.FSR_LISTING_URL)
    disc.assert_called_once_with(html="<html>", base_url=mod.FSR_LISTING_URL)
    kwargs = fp.call_args.kwargs
    assert kwargs["indicator_id"] == "bb_npl_structure"
    assert kwargs["snapshot_dir"] == tmp_path
    assert kwargs["period"] == (2026, 6)
