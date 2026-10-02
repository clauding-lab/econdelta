"""Unit tests for the DAM portal daily-price ticker parser.

Covers Bengali-digit translation, NFC normalization, and midpoint
extraction. The fixture mirrors the real DAM HTML structure: items
listed as `<bengali-label> :&nbsp;<low> - <high>` with Bengali digits.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.dam_ticker  # noqa: F401 — registers
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

# Realistic ticker fragment with mixed entity references and Bengali digits.
_HTML = (
    "<h1>Date of report: 24-09-2026</h1>"
    "<div class='ticker'>"
    "চিনি (দেশী) :&nbsp;১৩২.০০ - ১৩৫.০০ ▲০.০০% "
    "খামারের মুরগী :&nbsp;১৬২.০০ - ১৬৭.০০ ▲০.০০% "
    "সয়াবিন তেল :&nbsp;১৬৩.০০ - ১৬৫.০০ ▲০.০০% "
    "আমন চাল - মোটা :&nbsp;৪৮.০০ - ৫০.০০ ▲০.০০%"
    "</div>"
)


@pytest.fixture
def fixture_artifact(tmp_path: Path) -> FetchResult:
    p = tmp_path / "dam.html"
    p.write_text(_HTML, encoding="utf-8")
    return FetchResult(
        indicator_id="dam_food_test",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="http://market.dam.gov.bd/market_daily_price_report",
        sha256="x" * 64,
        cache_hit=False,
    )


def test_extracts_sugar_midpoint(fixture_artifact):
    parser = get_parser("dam_ticker")
    result = parser.parse(fixture_artifact, instruction="চিনি (দেশী)")
    # Mid of 132.00 - 135.00 = 133.5
    assert result.value == 133.5
    assert result._parse_strategy == "dam_ticker"


def test_extracts_chicken_midpoint(fixture_artifact):
    parser = get_parser("dam_ticker")
    result = parser.parse(fixture_artifact, instruction="খামারের মুরগী")
    # Mid of 162.00 - 167.00 = 164.5
    assert result.value == 164.5


def test_handles_decomposed_unicode_in_instruction(fixture_artifact):
    """Real HTML uses single-codepoint য় (U+09DF). The instruction string
    might be typed as decomposed য + ◌় (U+09AF + U+09BC). NFC normalization
    on both sides keeps the match working."""
    parser = get_parser("dam_ticker")
    # সয়াবিন with য় as decomposed pair (U+09AF + U+09BC)
    decomposed = "সয়াবিন তেল"
    result = parser.parse(fixture_artifact, instruction=decomposed)
    assert result.value == 164.0


def test_raises_on_unknown_label(fixture_artifact):
    parser = get_parser("dam_ticker")
    with pytest.raises(ParseError, match="not found"):
        parser.parse(fixture_artifact, instruction="পেঁয়াজ - দেশী")  # not in fixture


def test_rejects_legacy_ticker_without_report_date(tmp_path):
    p = tmp_path / "undated.html"
    p.write_text("আমন চাল - মোটা : ৪৮ - ৫০", encoding="utf-8")
    artifact = FetchResult("dam_food_test", p, "html", datetime.now(timezone.utc),
                           "https://dam.gov.bd/", "x" * 64, False)
    with pytest.raises(ParseError, match="report date"):
        get_parser("dam_ticker").parse(artifact, instruction="আমন চাল - মোটা")


@pytest.mark.parametrize("raw_date", ["24-09-2026", "২৪-০৯-২০২৬", "24/09/2026"])
def test_dam_report_date_accepts_bengali_digits_and_separators(tmp_path, raw_date):
    p = tmp_path / "dated.html"
    p.write_text(f"Date of report: {raw_date} আমন চাল - মোটা : ৪৭.৪৫ - ৫০.৪৮", encoding="utf-8")
    artifact = FetchResult("dam_food_test", p, "html", datetime.now(timezone.utc),
                           "https://dam.gov.bd/", "x" * 64, False)
    result = get_parser("dam_ticker").parse(artifact, instruction="আমন চাল - মোটা")
    assert result.source_as_of.isoformat() == "2026-09-24"


def test_dam_ticker_rejects_report_and_banner_date_mismatch(tmp_path):
    p = tmp_path / "mismatch.html"
    p.write_text(
        "Date of report: 24-09-2026 Daily average retail price: 25-09-2026 "
        "আমন চাল - মোটা : ৪৭.৪৫ - ৫০.৪৮", encoding="utf-8"
    )
    artifact = FetchResult("dam_food_test", p, "html", datetime.now(timezone.utc),
                           "https://dam.gov.bd/", "x" * 64, False)
    with pytest.raises(ParseError, match="dates disagree"):
        get_parser("dam_ticker").parse(artifact, instruction="আমন চাল - মোটা")


def test_new_portal_json_extracts_only_exact_dated_retail_identity(tmp_path):
    import json

    p = tmp_path / "dam.json"
    p.write_text(json.dumps({"success": True, "data": [
        {"commodity_id": 604, "price_date": "2026-09-24",
         "a_r_lowestPrice": "47.45", "a_r_howestPrice": "50.48"},
        {"commodity_id": 604, "price_date": "2026-09-23",
         "a_r_lowestPrice": "47.00", "a_r_howestPrice": "50.00"},
    ]}), encoding="utf-8")
    artifact = FetchResult("food_rice_coarse", p, "html", datetime.now(timezone.utc),
                           "https://moa-services.com/", "x" * 64, False)
    result = get_parser("dam_ticker").parse(artifact, instruction="commodity_id=604 unit_retail=2")
    assert result.value == 48.97
    assert result.source_as_of.isoformat() == "2026-09-24"
    assert result.unit == "BDT/kg"


def test_verified_egg_price_unit_retains_currency_and_pack_size(tmp_path):
    import json

    p = tmp_path / "dam.json"
    p.write_text(json.dumps({"success": True, "data": [
        {"commodity_id": 682, "price_date": "2026-09-24",
         "a_r_lowestPrice": "52.00", "a_r_howestPrice": "56.00"},
    ]}), encoding="utf-8")
    artifact = FetchResult("food_egg_red", p, "html", datetime.now(timezone.utc),
                           "https://moa-services.com/", "x" * 64, False)
    result = get_parser("dam_ticker").parse(artifact, instruction="commodity_id=682 unit_retail=5")

    assert result.value == 54.0
    assert result.unit == "BDT/4 pieces"


def test_new_portal_json_browser_viewer_wrapper_is_supported(tmp_path):
    import json

    payload = {"success": True, "data": [
        {"commodity_id": 604, "price_date": "2026-09-24",
         "a_r_lowestPrice": "47.45", "a_r_howestPrice": "50.48"},
    ]}
    p = tmp_path / "dam.html"
    p.write_text(f"<html><body><pre>{json.dumps(payload)}</pre></body></html>", encoding="utf-8")
    artifact = FetchResult("food_rice_coarse", p, "html", datetime.now(timezone.utc),
                           "https://moa-services.com/", "x" * 64, False)
    result = get_parser("dam_ticker").parse(artifact, instruction="commodity_id=604 unit_retail=2")
    assert result.value == 48.97
    assert result.unit == "BDT/kg"


def test_dated_dam_observation_writes_price_unit_and_own_period(tmp_path, monkeypatch):
    import json

    import parsers.hybrid as hybrid

    registry = json.loads((Path(__file__).parents[1] / "config/sources-v3.json").read_text())
    indicator = next(item for item in registry["indicators"] if item["id"] == "food_rice_coarse")
    payload = {"success": True, "data": [
        {"commodity_id": 604, "price_date": "2026-09-24",
         "a_r_lowestPrice": "47.45", "a_r_howestPrice": "50.48"},
        {"commodity_id": 628, "price_date": "2026-09-24",
         "a_r_lowestPrice": "52.00", "a_r_howestPrice": "55.00"},
    ]}
    p = tmp_path / "dam.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    artifact = FetchResult("food_rice_coarse", p, "html", datetime.now(timezone.utc),
                           "https://moa-services.com/", "x" * 64, False)
    monkeypatch.setattr(hybrid, "_sanity_check", lambda **kwargs: type(
        "Result", (), {"parsed": {"plausible": True, "reason": "ok"}})())

    snapshot = hybrid.parse_one(artifact, indicator, history=[])

    assert snapshot["value"] == 48.97
    assert snapshot["unit"] == "BDT/kg"
    assert snapshot["source_as_of"] == "2026-09-24"


def test_rejected_dam_period_does_not_recover_sibling_period_or_use_llm(tmp_path, monkeypatch):
    import json

    import parsers.hybrid as hybrid

    registry = json.loads((Path(__file__).parents[1] / "config/sources-v3.json").read_text())
    indicator = next(item for item in registry["indicators"] if item["id"] == "food_rice_coarse")
    payload = {"success": True, "data": [
        {"commodity_id": 604, "price_date": "2026-09-23",
         "a_r_lowestPrice": "47.00", "a_r_howestPrice": "50.00"},
        {"commodity_id": 628, "price_date": "2026-09-24",
         "a_r_lowestPrice": "52.00", "a_r_howestPrice": "55.00"},
    ]}
    p = tmp_path / "dam.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    artifact = FetchResult("food_rice_coarse", p, "html", datetime.now(timezone.utc),
                           "https://moa-services.com/", "x" * 64, False)
    assert "llm_prompt" not in indicator["parse"]
    monkeypatch.setattr(hybrid, "_llm_extract", lambda **kwargs: pytest.fail("must not infer a DAM period"))

    snapshot = hybrid.parse_one(artifact, indicator, history=[])

    assert snapshot["_provenance"] == "needs_review"
    assert "source_as_of" not in snapshot


@pytest.mark.parametrize("instruction,payload", [
    ("commodity_id=818 unit_retail=2", {"success": True, "data": [
        {"commodity_id": 818, "price_date": "2026-09-24", "a_r_lowestPrice": "1", "a_r_howestPrice": "2"}]}),
    ("commodity_id=604 unit_retail=2", {"success": True, "data": [
        {"commodity_id": 604, "price_date": "2026-09-23", "a_r_lowestPrice": "1", "a_r_howestPrice": "2"},
        {"commodity_id": 628, "price_date": "2026-09-24", "a_r_lowestPrice": "3", "a_r_howestPrice": "4"}]}),
    ("commodity_id=604 unit_retail=2", {"success": True, "report_date": "2026-09-25", "data": [
        {"commodity_id": 604, "price_date": "2026-09-24", "a_r_lowestPrice": "1", "a_r_howestPrice": "2"}]}),
    ("commodity_id=604 unit_retail=2", {"success": True, "data": [
        {"commodity_id": 604, "a_r_lowestPrice": "1", "a_r_howestPrice": "2"}]}),
])
def test_new_portal_rejects_wrong_identity_conflicting_dates_or_missing_date(
    tmp_path, instruction, payload
):
    import json

    p = tmp_path / "dam.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    artifact = FetchResult("food_test", p, "html", datetime.now(timezone.utc),
                           "https://moa-services.com/", "x" * 64, False)
    with pytest.raises(ParseError):
        get_parser("dam_ticker").parse(artifact, instruction=instruction)


def test_registry_pins_only_verified_dam_product_and_unit_pairs():
    registry = json.loads((Path(__file__).parents[1] / "config/sources-v3.json").read_text())
    definitions = json.loads((Path(__file__).parent / "fixtures/dam_price_definitions_20260924.json").read_text())
    products = {row["value"]: row for row in definitions["data"]["commodityNameList"]}
    units = {row["value"]: row["text_en"].strip() for row in definitions["data"]["measurementUnitList"]}
    expected = {
        "food_rice_coarse": (604, 2, "Rice - Aman - Coarse"),
        "food_atta_packet": (628, 2, "Ata (Packet)"),
        "food_egg_red": (682, 5, "Egg Farm-Red"),
        "food_chicken_farm": (676, 2, "Farm-raised Hen"),
        "food_onion_local": (831, 2, "Onion (local)"),
        "food_sugar_local": (760, 2, "Sugar (Local)"),
    }
    indicators = {item["id"]: item for item in registry["indicators"]}
    for indicator_id, (commodity_id, unit_id, name) in expected.items():
        task = indicators[indicator_id]["fetch"]["task"]
        assert task == f"commodity_id={commodity_id} unit_retail={unit_id}"
        assert indicators[indicator_id]["fetch"]["url"].endswith("daily-price-scroll")
        assert "llm_prompt" not in indicators[indicator_id]["parse"]
        assert products[commodity_id]["text_en"] == name
        assert products[commodity_id]["unit_retail"] == unit_id
        assert units[unit_id] in {"Kilogram", "4 Pieces"}

    # Generic legacy contracts do not identify oil packaging or lentil origin/grade.
    for indicator_id in ("food_oil_soybean", "food_lentil_moong"):
        assert "commodity_id=" not in indicators[indicator_id]["fetch"]["task"]
        assert "market.dam.gov.bd" in indicators[indicator_id]["fetch"]["url"]
        assert "llm_prompt" not in indicators[indicator_id]["parse"]
