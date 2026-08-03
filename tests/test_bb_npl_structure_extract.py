"""tests/test_bb_npl_structure_extract.py"""
from unittest.mock import MagicMock, patch

import pytest


def test_prompt_names_every_key_and_demands_verbatim_billions():
    from scrapers.bb_npl_structure import FSR_EXTRACTION_KEYS, build_extraction_prompt
    prompt = build_extraction_prompt("TABLE WINDOW TEXT")
    for key in FSR_EXTRACTION_KEYS:
        assert key in prompt
    assert "null" in prompt
    assert "billion" in prompt.lower()      # verbatim billions, no conversion
    assert "TABLE WINDOW TEXT" in prompt


def test_run_extraction_returns_parsed_dict():
    from scrapers.bb_npl_structure import run_extraction
    ok = MagicMock(parsed={"overall_npl_ratio_fsr": 30.60})
    with patch("scrapers.bb_npl_structure.run_max", return_value=ok) as rm:
        assert run_extraction("w") == {"overall_npl_ratio_fsr": 30.60}
    assert rm.call_count == 1


def test_run_extraction_retries_once_then_raises():
    from scrapers.bb_npl_structure import ExtractionError, run_extraction
    bad = MagicMock(parsed=None, raw_text="prose")
    with patch("scrapers.bb_npl_structure.run_max", return_value=bad) as rm:
        with pytest.raises(ExtractionError):
            run_extraction("w")
    assert rm.call_count == 2


def test_run_extraction_wraps_maxcallerror():
    from claude_max.max_client import MaxCallError
    from scrapers.bb_npl_structure import ExtractionError, run_extraction
    with patch("scrapers.bb_npl_structure.run_max", side_effect=MaxCallError("boom")):
        with pytest.raises(ExtractionError):
            run_extraction("w")
