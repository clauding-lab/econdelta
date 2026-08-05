from datetime import datetime, timezone
from unittest.mock import patch

import parsers.html_footer_ticker  # noqa: F401 — registers
from fetchers.base import FetchResult
from parsers.hybrid import parse_one


def _ticker_artifact(tmp_path):
    p = tmp_path / "x.html"
    p.write_text("<html><body>Policy Rate 10.0%</body></html>")
    return FetchResult(
        indicator_id="policy_rate_repo", artifact_path=p, artifact_type="html",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False,
    )


def test_deterministic_path_emits_value_when_sonnet_agrees(tmp_path):
    indicator = {
        "id": "policy_rate_repo", "name": "Policy Rate", "domain": "money_market",
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


def test_falls_back_to_llm_when_deterministic_raises_bare_value_error(tmp_path):
    """Audit E23 defect A/B: a plain ValueError from a deterministic parser
    (e.g. _to_number on '2025-26') must land in the ladder's LLM fallback,
    not escape parse_one and skip the snapshot entirely."""
    indicator = {
        "id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
        "fetch": {"task": "Policy Rate"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                  "valid_range": [0.0, 100.0], "llm_prompt": "html_footer_ticker.txt"},
    }
    def _raise_bare_value_error(*a, **k):
        raise ValueError("boom")

    fake_extract = type("R", (), {"parsed": {"value": 7.0}, "raw_text": ""})()
    with patch("parsers.hybrid.get_parser") as fake_get_parser:
        fake_parser = type("P", (), {"parse": staticmethod(_raise_bare_value_error)})()
        fake_get_parser.return_value = fake_parser
        with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
            snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[])
    assert snapshot["value"] == 7.0
    assert snapshot["_provenance"] == "llm_extracted"


def test_llm_extract_string_value_goes_to_needs_review_not_unvalidated_snapshot(tmp_path):
    """Audit E23 defect C: a non-numeric LLM extract (str/list) must not skip
    validate_value and enter the snapshot as-is — it must land on the
    extract_failed/needs_review sentinel path."""
    indicator = {
        "id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
        "fetch": {"task": "Nonexistent"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                  "valid_range": [0.0, 100.0], "llm_prompt": "html_footer_ticker.txt"},
    }
    fake_extract = type("R", (), {"parsed": {"value": "not a number"}, "raw_text": "not a number"})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"


def test_llm_extract_bool_value_is_rejected_not_cast_to_float(tmp_path):
    """Audit E23 defect C: bool is an int subclass so isinstance(v_llm, (int,
    float)) passes it through, and float(True) == 1.0 loses the type info
    validate_value's bool guard checks — the guard must fire before the cast."""
    indicator = {
        "id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
        "fetch": {"task": "Nonexistent"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                  "valid_range": [0.0, 100.0], "llm_prompt": "html_footer_ticker.txt"},
    }
    fake_extract = type("R", (), {"parsed": {"value": True}, "raw_text": "true"})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"
