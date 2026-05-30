import pytest

from briefing.prompt import BriefingValidationError, build_prompt, validate_output

VALID_IDS = {"call_money_rate:change", "tbond_5y_yield:zscore"}


def _ok_output():
    return {
        "title": "The short end is rotating",
        "body": "Three forces...",
        "featured_anomalies": [{"candidate_id": "call_money_rate:change", "why": "VAT outflow"}],
        "updated_threads": [{"id": "t-reserves", "thread": "Reserves vs IMF floor",
                             "status": "open", "since_week": "2026-W20", "note": "3rd week"}],
    }


def test_validate_accepts_good_output():
    out = validate_output(_ok_output(), VALID_IDS)
    assert out["title"] == "The short end is rotating"


def test_validate_rejects_unknown_candidate_id():
    bad = _ok_output()
    bad["featured_anomalies"][0]["candidate_id"] = "made_up_metric:change"
    with pytest.raises(BriefingValidationError, match="unknown candidate_id"):
        validate_output(bad, VALID_IDS)


def test_validate_rejects_none():
    with pytest.raises(BriefingValidationError, match="not JSON"):
        validate_output(None, VALID_IDS)


def test_validate_rejects_missing_title():
    bad = _ok_output()
    del bad["title"]
    with pytest.raises(BriefingValidationError, match="title"):
        validate_output(bad, VALID_IDS)


def test_validate_rejects_bad_thread_status():
    bad = _ok_output()
    bad["updated_threads"][0]["status"] = "maybe"
    with pytest.raises(BriefingValidationError, match="status"):
        validate_output(bad, VALID_IDS)


def test_build_prompt_includes_week_and_candidate_ids():
    p = build_prompt(digest={"call_money_rate": {"latest": 9.34}},
                     candidates=[{"candidate_id": "call_money_rate:change", "detail": "x"}],
                     prior_briefings=[], open_threads=[], week_of="2026-06-01")
    assert "2026-06-01" in p
    assert "call_money_rate:change" in p
    assert "Markdown" in p  # body must instruct segmented markdown (sub-headings + bullets)


def test_validate_rejects_empty_why():
    bad = _ok_output()
    bad["featured_anomalies"][0]["why"] = "   "
    with pytest.raises(BriefingValidationError, match="why"):
        validate_output(bad, VALID_IDS)


def test_build_prompt_trims_oversized_prior_to_valid_json():
    import json
    huge = [{"week_of": f"2026-W{i:02d}", "body": "x" * 50_000} for i in range(8)]
    p = build_prompt(digest={}, candidates=[], prior_briefings=huge,
                     open_threads=[], week_of="2026-06-01")
    block = p.split("PRIOR BRIEFINGS (newest first):\n")[1].split("\n\nOPEN THREADS:")[0]
    parsed = json.loads(block)  # raises if mid-object truncated
    assert len(parsed) < 8  # some oldest briefings were dropped to fit
