from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import parsers.html_footer_ticker  # noqa: F401 — registers
from fetchers.base import FetchResult
from parsers.hybrid import parse_one


def _ticker_artifact(tmp_path):
    p = tmp_path / "x.html"
    p.write_text("<html><body>Policy Rate 10.0%</body></html>")
    return FetchResult(
        indicator_id="policy_rate_slf_sdf", artifact_path=p, artifact_type="html",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False,
    )


def test_deterministic_path_emits_value_when_sonnet_agrees(tmp_path):
    indicator = {
        "id": "policy_rate_slf_sdf", "name": "Policy Rate", "domain": "money_market",
        "cadence": "daily",
        "fetch": {"task": "Policy Rate"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                  "valid_range": [0.5, 25.0], "llm_prompt": "html_footer_ticker.txt"},
    }
    fake_sanity = type("R", (), {"parsed": {"plausible": True, "reason": "ok"}, "raw_text": ""})()
    with patch("parsers.hybrid._sanity_check", return_value=fake_sanity):
        snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[])
    assert snapshot["value"] == 10.0
    assert snapshot["_provenance"] == "deterministic"


def test_falls_back_to_llm_when_deterministic_raises(tmp_path):
    indicator = {
        "id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
        "fetch": {"task": "Nonexistent"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                  "valid_range": [0.0, 100.0], "llm_prompt": "html_footer_ticker.txt"},
    }
    fake_extract = type("R", (), {"parsed": {"value": 7.0}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[])
    assert snapshot["value"] == 7.0
    assert snapshot["_provenance"] == "llm_extracted"
