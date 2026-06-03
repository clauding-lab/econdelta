from datetime import date
from unittest.mock import patch

from media_screen.extract import extract_numbers
from media_screen.types import MetricSpec

SPECS = [MetricSpec("gross_npl_ratio", ("NPL ratio", "default loan"), 0.05)]


def test_extracts_value_and_period():
    fake = type("R", (), {"parsed": {"findings": [
        {"press_name": "NPL ratio", "value": 32.26, "period": "2026-03-31",
         "quote": "NPLs were 32.26% as of end-March 2026."}
    ]}, "raw_text": ""})()
    with patch("media_screen.extract.run_max", return_value=fake):
        out = extract_numbers("article text", specs=SPECS,
                              source_url="http://x", source_outlet="tbsnews")
    assert len(out) == 1
    assert out[0].value == 32.26 and out[0].period == date(2026, 3, 31)
    assert out[0].indicator_hint == "NPL ratio"


def test_undated_finding_keeps_period_none():
    fake = type("R", (), {"parsed": {"findings": [
        {"press_name": "NPL ratio", "value": 32.26, "period": None, "quote": "NPLs rose."}
    ]}, "raw_text": ""})()
    with patch("media_screen.extract.run_max", return_value=fake):
        out = extract_numbers("t", specs=SPECS, source_url="http://x", source_outlet="tbs")
    assert out[0].period is None  # downstream filter discards it


def test_llm_error_returns_empty(caplog):
    from claude_max.max_client import MaxCallError
    with patch("media_screen.extract.run_max", side_effect=MaxCallError("boom")):
        out = extract_numbers("t", specs=SPECS, source_url="http://x", source_outlet="tbs")
    assert out == []  # screen fails safe — no candidates
