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


# ---------------------------------------------------------------------------
# No llm_prompt configured (cab-memo-2026-08-05.md D2 bundle): the ladder
# must degrade gracefully to the terminal fallback, never raise KeyError on
# the missing config key.
# ---------------------------------------------------------------------------


def test_no_llm_prompt_and_deterministic_fails_uses_terminal_fallback(tmp_path):
    indicator = {
        "id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
        "fetch": {"task": "Nonexistent"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "amount_usd_bn",
                  "valid_range": [-20.0, 20.0]},  # no llm_prompt key
    }
    snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[], last_good=None)
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"
    assert snapshot["value"] == 0.0


def test_no_llm_prompt_total_failure_on_money_metric_alerts_same_day(tmp_path):
    """cab-memo review MED-1: a no-LLM money metric going completely dark
    must not go quiet at INFO — logs ERROR and fires a Discord notify so a
    human finds out same-day (Stage 3's own failure counter won't catch a
    stale_fallback snapshot; it isn't 'bad' by _is_bad_snapshot's definition)."""
    indicator = {
        "id": "x", "name": "X", "domain": "macro", "cadence": "monthly",
        "fetch": {"task": "Nonexistent"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "amount_usd_bn",
                  "valid_range": [-20.0, 20.0]},  # no llm_prompt key
    }
    with patch("parsers.hybrid.notify") as fake_notify:
        snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[], last_good=None)
    assert snapshot["_provenance"] == "needs_review"
    fake_notify.assert_called_once()
    level, title, message = fake_notify.call_args.args[:3]
    assert level == "warning"
    assert "x" in title


def test_no_llm_prompt_total_failure_on_non_money_metric_does_not_alert(tmp_path):
    """The MED-1 escalation is scoped to money (amount_*) value types — a
    percent/rate metric with no llm_prompt keeps the quieter INFO-level log
    it already had; only money metrics get the same-day Discord alert."""
    indicator = {
        "id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
        "fetch": {"task": "Nonexistent"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                  "valid_range": [0.0, 100.0]},  # no llm_prompt key
    }
    with patch("parsers.hybrid.notify") as fake_notify:
        parse_one(_ticker_artifact(tmp_path), indicator, history=[], last_good=None)
    fake_notify.assert_not_called()


def test_no_llm_prompt_and_sanity_check_disagrees_flags_dont_veto(tmp_path):
    """cab-memo review HIGH-2: a sanity-check 'implausible' with no
    llm_prompt configured has no cross-check escape hatch. A false positive
    on a genuine step-change month must not silently discard the (already
    validated) deterministic value — needs_review would make Stage 3 drop
    it, and _load_last_good skips needs_review snapshots, freezing the
    metric forever. Must FLAG (publish as deterministic + note + alert),
    never VETO (needs_review), and never raise KeyError reaching for a
    missing 'llm_prompt' config key."""
    indicator = {
        "id": "policy_rate_repo", "name": "Policy Rate", "domain": "money_market",
        "cadence": "daily",
        "fetch": {"task": "Policy Rate"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                  "valid_range": [0.5, 25.0]},  # no llm_prompt key
    }
    fake_sanity = type("R", (), {"parsed": {"plausible": False, "reason": "looks odd"}, "raw_text": ""})()
    with patch("parsers.hybrid._sanity_check", return_value=fake_sanity), \
         patch("parsers.hybrid.notify") as fake_notify:
        snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[])
    assert snapshot["value"] == 10.0
    assert snapshot["_provenance"] == "deterministic"
    assert snapshot["_parse_strategy"] == "html_footer_ticker"
    assert "looks odd" in snapshot["sanity_note"]
    fake_notify.assert_called_once()
    level, title, message = fake_notify.call_args.args[:3]
    assert level == "warning"
    assert "policy_rate_repo" in title


def test_terminal_fallback_holds_last_good_for_amount_value_type(tmp_path):
    indicator = {
        "id": "x", "name": "X", "domain": "macro", "cadence": "monthly",
        "fetch": {"task": "Nonexistent"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "amount_usd_bn",
                  "valid_range": [-20.0, 20.0]},
    }
    last_good = {
        "indicator_id": "x", "value": -0.301, "scraped_at": "2026-08-04T08:00:59+00:00",
        "_provenance": "deterministic", "_parse_strategy": "html_footer_ticker",
        "_stale_from": "2026-08-04",
    }
    snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[], last_good=last_good)
    assert snapshot["value"] == -0.301
    assert snapshot["_provenance"] == "stale_fallback"
    assert snapshot["scraped_at"] == "2026-08-04T08:00:59+00:00"  # ORIGINAL date, not today


def test_terminal_fallback_does_not_hold_last_good_for_non_amount_value_type(tmp_path):
    """Hold-last-good is scoped to amount_* value types (money) per the memo
    bundle — a percent/rate/ratio metric keeps the original 0.0/needs_review
    sentinel even when a last_good value is available."""
    indicator = {
        "id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
        "fetch": {"task": "Nonexistent"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                  "valid_range": [0.0, 100.0]},
    }
    last_good = {
        "indicator_id": "x", "value": 9.5, "scraped_at": "2026-08-04T08:00:59+00:00",
        "_provenance": "deterministic", "_parse_strategy": "html_footer_ticker",
    }
    snapshot = parse_one(_ticker_artifact(tmp_path), indicator, history=[], last_good=last_good)
    assert snapshot["value"] == 0.0
    assert snapshot["_provenance"] == "needs_review"


# ---------------------------------------------------------------------------
# call_money_rate freeze (2026-08-08): the LLM-only fallback path (used when
# the deterministic parser fails outright) rejected ANY non-int/float extract
# value, including the legitimate multi-tenor dict call_money_rate's LLM
# extraction returns — e.g. {"1D": 9.48, "7D": 11.95, "14D": 9.48, "90D": null}
# (real shape observed on the box, Aug 3-4 snapshots). PR #107 closed a real
# hole for scalar indicators but had no dict-aware branch here, so the whole
# bundle was rejected wholesale and the metric froze on stale_fallback.
# ---------------------------------------------------------------------------

_CALL_MONEY_INDICATOR = {
    "id": "call_money_rate", "name": "Call money rate", "domain": "money_market",
    "cadence": "daily",
    "fetch": {"task": "Nonexistent"},  # forces the deterministic parser to fail
    "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
              "valid_range": [0.0, 25.0], "llm_prompt": "html_footer_ticker.txt"},
}


def test_llm_extract_multi_tenor_dict_reproduces_and_fixes_the_freeze(tmp_path):
    """Reproduces the Aug 5-7 freeze: the real 4-tenor dict shape the LLM
    extraction legitimately returns must be accepted through the LLM-only
    fallback path, not rejected wholesale into needs_review/extract_failed."""
    real_shape = {"1D": 9.48, "7D": 11.95, "14D": 9.48, "90D": None}
    fake_extract = type("R", (), {"parsed": {"value": real_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "llm_extracted"
    assert snapshot["_parse_strategy"] == "html_footer_ticker"
    assert snapshot["value"] == real_shape


def test_llm_extract_all_numeric_dict_tenors_accepted(tmp_path):
    real_shape = {"1D": 9.48, "7D": 11.95, "14D": 9.48, "90D": 9.75}
    fake_extract = type("R", (), {"parsed": {"value": real_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "llm_extracted"
    assert snapshot["value"] == real_shape


def test_llm_extract_dict_null_tenor_passes_and_is_preserved(tmp_path):
    """A null tenor (e.g. 90D not traded that day) is a valid per-key shape —
    it is skipped downstream by aggregate_latest._flatten_dict_indicators,
    not rejected here."""
    real_shape = {"1D": 9.48, "7D": 11.95, "14D": 9.48, "90D": None}
    fake_extract = type("R", (), {"parsed": {"value": real_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "llm_extracted"
    assert snapshot["value"]["90D"] is None


def test_llm_extract_dict_string_tenor_rejected(tmp_path):
    bad_shape = {"1D": 9.48, "7D": "n/a", "14D": 9.48, "90D": None}
    fake_extract = type("R", (), {"parsed": {"value": bad_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"


def test_llm_extract_dict_bool_tenor_rejected(tmp_path):
    """bool is an int subclass — must be rejected per-key exactly like the
    scalar path rejects a bool value (test_llm_extract_bool_value_is_rejected_
    not_cast_to_float above)."""
    bad_shape = {"1D": 9.48, "7D": True, "14D": 9.48, "90D": None}
    fake_extract = type("R", (), {"parsed": {"value": bad_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"


def test_llm_extract_dict_nested_tenor_rejected(tmp_path):
    bad_shape = {"1D": 9.48, "7D": {"low": 9.0, "high": 10.0}, "14D": 9.48, "90D": None}
    fake_extract = type("R", (), {"parsed": {"value": bad_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"


def test_llm_extract_empty_dict_rejected(tmp_path):
    fake_extract = type("R", (), {"parsed": {"value": {}}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"


# ---------------------------------------------------------------------------
# PR #121 review — CRITICAL/IMPORTANT: the dict branch above skipped ALL
# per-key range validation, key allow-listing, and finiteness checking. These
# tests reproduce each hole exactly as described in the review before the fix
# and must be GREEN only once hybrid.py validates per key.
# ---------------------------------------------------------------------------

def test_llm_extract_dict_out_of_range_tenor_rejected(tmp_path):
    """A hallucinated decimal-point misread (9.48 -> 948.0) must not publish —
    call_money_rate's valid_range is [0.0, 25.0]."""
    bad_shape = {"1D": 948.0, "7D": 11.95, "14D": 9.48, "90D": None}
    fake_extract = type("R", (), {"parsed": {"value": bad_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"
    assert snapshot["value"] != bad_shape


def test_llm_extract_dict_negative_tenor_rejected(tmp_path):
    bad_shape = {"1D": -5.0, "7D": 11.95, "14D": 9.48, "90D": None}
    fake_extract = type("R", (), {"parsed": {"value": bad_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"


def test_llm_extract_dict_nan_tenor_rejected(tmp_path):
    """NaN passes isinstance(x, float) so a bare isinstance guard lets it
    through — must be caught by the range check instead."""
    bad_shape = {"1D": float("nan"), "7D": 11.95, "14D": 9.48, "90D": None}
    fake_extract = type("R", (), {"parsed": {"value": bad_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"


def test_llm_extract_dict_infinity_tenor_rejected(tmp_path):
    bad_shape = {"1D": 9.48, "7D": float("inf"), "14D": 9.48, "90D": None}
    fake_extract = type("R", (), {"parsed": {"value": bad_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"


def test_llm_extract_dict_unexpected_key_dropped_not_published(tmp_path):
    """A key outside call_money_rate's tenor allow-list (path-traversal-shaped
    or otherwise) must never reach metric_history as a minted metric_id."""
    bad_shape = {"1D": 9.48, "7D": 11.95, "14D": 9.48, "90D": None, "../evil": 1.0}
    fake_extract = type("R", (), {"parsed": {"value": bad_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "llm_extracted"
    assert "../evil" not in snapshot["value"]
    assert snapshot["value"] == {"1D": 9.48, "7D": 11.95, "14D": 9.48, "90D": None}


def test_llm_extract_dict_casing_drift_normalized_to_canonical_key(tmp_path):
    """A lower-cased tenor key ('1d' instead of '1D') must still land under
    the canonical '1D' key so aggregate_latest's exact-case
    call_money.get("1D") headline promotion actually fires."""
    shape = {"1d": 9.48, "7D": 11.95, "14D": 9.48, "90D": None}
    fake_extract = type("R", (), {"parsed": {"value": shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "llm_extracted"
    assert snapshot["value"]["1D"] == 9.48
    assert "1d" not in snapshot["value"]


def test_llm_extract_dict_all_keys_unrecognized_rejected(tmp_path):
    """Every key fails the allow-list (free-text LLM tenor labels) — must
    reject the whole snapshot rather than publish an empty/partial dict."""
    bad_shape = {"overnight": 9.48, "7 Day": 11.95, "../evil": 1.0}
    fake_extract = type("R", (), {"parsed": {"value": bad_shape}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(_ticker_artifact(tmp_path), _CALL_MONEY_INDICATOR, history=[])
    assert snapshot["_provenance"] == "needs_review"
    assert snapshot["_parse_strategy"] == "extract_failed"


def test_llm_extract_dict_rejected_for_scalar_indicator():
    """A scalar money indicator (monthly_remittance) hallucinating a dict
    must NOT silently 'succeed' as llm_extracted — it must fall through to
    the scalar rejection path so hold-last-good (money) can fire."""
    indicator = {
        "id": "monthly_remittance", "name": "Monthly remittance", "domain": "external",
        "cadence": "monthly",
        "fetch": {"task": "Nonexistent"},
        "parse": {"deterministic": "html_footer_ticker", "value_type": "amount_usd_bn",
                  "valid_range": [0.0, 10.0], "llm_prompt": "html_footer_ticker.txt"},
    }
    artifact = FetchResult(
        indicator_id="monthly_remittance", artifact_path=Path("/dev/null"), artifact_type="html",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x" * 64, cache_hit=False,
    )
    fake_extract = type("R", (), {"parsed": {"value": {"amount": 2.5, "unit": 1}}, "raw_text": ""})()
    with patch("parsers.hybrid._llm_extract", return_value=fake_extract):
        snapshot = parse_one(artifact, indicator, history=[], last_good=None)
    assert snapshot["_provenance"] != "llm_extracted"
    assert snapshot["_parse_strategy"] == "extract_failed"
