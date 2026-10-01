"""Unit tests for the DSE sector-heat parser.

Since 2026-10-01 the parser reads DSE's ``/api/live/prices`` JSON (the old
``recent_market_information.php`` page returns HTTP 410). It maps each scrip's
``percent`` through ``cols``, aggregates by sector via simple average using
config/dse_sector_constituents.json, and dates the result with the closed
session's ``session.sessionDate``.

``dse_api_live_prices.json`` is a real capture taken after the 2026-10-01
close (634 rows; ``percent`` null for untraded scrips and debt boards).
"""
import html
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import aggregate_latest as agg
import parsers.dse_sector_heat  # noqa: F401 — registers
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dse_api_live_prices.json"
COLS = ["code", "ltp", "ycp", "open", "high", "low", "close", "volume", "value",
        "trades", "percent", "category", "board", "sector", "assetType"]
CLOSED = {"isOpen": False, "phase": "closed", "tradingDay": True,
          "date": "2026-10-01", "sessionDate": "2026-10-01"}


def _row(code: str, pct: float | None) -> list:
    return [code, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 1, 1.0, 1, pct,
            "A", "PUBLIC", "Bank", "EQ"]


def _payload(rows: list[list], session: dict | None = None, cols=COLS) -> dict:
    return {"cols": cols, "rows": rows, "session": CLOSED if session is None else session}


_SYNTH_ROWS = [
    # Banks (all decline) — avg -1.4%
    _row("BRACBANK", -1.50), _row("DUTCHBANGL", -1.30), _row("EBL", -1.40),
    # NBFI — avg -1.1%
    _row("IDLC", -1.20), _row("IPDC", -1.00),
    # Pharma — avg +0.4%
    _row("SQURPHARMA", 0.50), _row("BEXIMCO", 0.30),
    # IT — one traded (+0.1), one untraded (null must NOT count as 0)
    _row("BDCOM", 0.10), _row("GENEXIL", None),
    # An UNKNOWN scrip not in the taxonomy — must be ignored
    _row("UNKNOWNSCRIP", 5.00),
]


def _chromium_json_view(raw: str) -> str:
    """What Playwright's page.content() returns for a JSON URL (seen via
    Chromium --dump-dom on the live endpoint, 2026-10-01)."""
    return (
        '<html><head><meta name="color-scheme" content="light dark"></head><body>'
        '<pre style="word-wrap: break-word; white-space: pre-wrap;">'
        f"{html.escape(raw, quote=False)}</pre>"
        '<div class="json-formatter-container"></div></body></html>'
    )


def _artifact(tmp_path: Path, text: str, name: str = "dse-prices.html") -> FetchResult:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return FetchResult(
        indicator_id="dse_sector_heat",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.dse.com.bd/api/live/prices",
        sha256="x" * 64,
        cache_hit=False,
    )


@pytest.fixture
def synth_artifact(tmp_path: Path) -> FetchResult:
    return _artifact(tmp_path, json.dumps(_payload(_SYNTH_ROWS)))


def test_extracts_per_scrip_pct_changes_and_skips_nulls():
    from parsers.dse_sector_heat import _parse_scrip_pcts
    pcts = _parse_scrip_pcts(_payload(_SYNTH_ROWS))
    assert pcts["BRACBANK"] == -1.50
    assert pcts["SQURPHARMA"] == 0.50
    assert pcts["UNKNOWNSCRIP"] == 5.00
    assert "GENEXIL" not in pcts  # null percent = untraded, never 0


def test_columns_are_mapped_by_name_not_position():
    from parsers.dse_sector_heat import _parse_scrip_pcts
    cols = ["percent", "code"]  # reordered upstream
    assert _parse_scrip_pcts({"cols": cols, "rows": [[-2.5, "GP"]]}) == {"GP": -2.5}


def test_aggregates_to_sector_dict(synth_artifact):
    result = get_parser("dse_sector_heat").parse(synth_artifact, instruction="")
    assert isinstance(result.value, dict)
    assert result.value["Banks"] == -1.40
    assert result.value["NBFI"] == -1.10
    assert result.value["Pharma"] == 0.40
    assert result.value["IT"] == 0.10  # avg of the ONE traded constituent


def test_source_as_of_is_session_date(synth_artifact):
    result = get_parser("dse_sector_heat").parse(synth_artifact, instruction="")
    assert result.source_as_of == date(2026, 10, 1)
    assert result._parse_strategy == "dse_sector_heat"


def test_session_date_not_calendar_date(tmp_path):
    """Friday run: session.date is the (non-trading) calendar day, sessionDate
    is Thursday's close — the value belongs to Thursday."""
    session = {**CLOSED, "tradingDay": False, "date": "2026-10-02",
               "sessionDate": "2026-10-01"}
    art = _artifact(tmp_path, json.dumps(_payload(_SYNTH_ROWS, session)))
    result = get_parser("dse_sector_heat").parse(art, instruction="")
    assert result.source_as_of == date(2026, 10, 1)


@pytest.mark.parametrize("is_open,phase", [
    (True, "open"), (False, "pre-open"), (False, "halted"), (False, "post-close"),
])
def test_refuses_unclosed_session(tmp_path, is_open, phase):
    session = {**CLOSED, "isOpen": is_open, "phase": phase}
    art = _artifact(tmp_path, json.dumps(_payload(_SYNTH_ROWS, session)))
    parser = get_parser("dse_sector_heat")
    with pytest.raises(ParseError, match="not closed"):
        parser.parse(art, instruction="")
    assert parser.recover_source_as_of(art) is None


def test_missing_session_date_raises(tmp_path):
    session = {k: v for k, v in CLOSED.items() if k != "sessionDate"}
    art = _artifact(tmp_path, json.dumps(_payload(_SYNTH_ROWS, session)))
    with pytest.raises(ParseError, match="sessionDate"):
        get_parser("dse_sector_heat").parse(art, instruction="")


def test_skips_sectors_with_no_constituents_in_data(synth_artifact):
    result = get_parser("dse_sector_heat").parse(synth_artifact, instruction="")
    assert "Telecom" not in result.value
    assert "Food" not in result.value


def test_unknown_scrips_dont_pollute_aggregation(synth_artifact):
    result = get_parser("dse_sector_heat").parse(synth_artifact, instruction="")
    assert all(pct < 1.0 for pct in result.value.values())


@pytest.mark.parametrize("text", [
    "<html><body>Gone</body></html>",                       # the 410 page
    json.dumps(_payload([])),                                # no rows
    json.dumps(_payload([_row("GENEXIL", None)])),           # only nulls
    json.dumps({"cols": ["code"], "rows": [], "session": CLOSED}),  # no percent col
])
def test_raises_when_no_usable_data(tmp_path, text):
    """ParseError so the hybrid orchestrator takes its designed fallback path."""
    with pytest.raises(ParseError):
        get_parser("dse_sector_heat").parse(_artifact(tmp_path, text), instruction="")


# --------------------------------------------------------------------------- #
# Real capture (2026-10-01 close)
# --------------------------------------------------------------------------- #

_EXPECTED_LIVE = {
    "Banks": -0.08, "NBFI": -0.59, "Textile": 1.49, "Pharma": -0.44,
    "Fuel": -0.69, "Telecom": 0.23, "Food": -1.26, "IT": -0.28,
}


def test_real_capture_raw_json(tmp_path):
    art = _artifact(tmp_path, FIXTURE.read_text(encoding="utf-8"), "prices.json")
    result = get_parser("dse_sector_heat").parse(art, instruction="")
    assert result.value == _EXPECTED_LIVE
    assert result.source_as_of == date(2026, 10, 1)


def test_real_capture_through_chromium_json_viewer(tmp_path):
    """The registry fetches via Playwright, which saves Chromium's <pre> view."""
    art = _artifact(tmp_path, _chromium_json_view(FIXTURE.read_text(encoding="utf-8")))
    parser = get_parser("dse_sector_heat")
    result = parser.parse(art, instruction="")
    assert result.value == _EXPECTED_LIVE
    assert parser.recover_source_as_of(art) == date(2026, 10, 1)


def test_real_capture_scrip_coverage():
    from parsers.dse_sector_heat import _load_payload, _parse_scrip_pcts
    pcts = _parse_scrip_pcts(_load_payload(FIXTURE.read_text(encoding="utf-8")))
    assert len(pcts) == 385  # 634 rows minus 249 null-percent rows
    assert pcts["GP"] == pytest.approx(-0.20695364238410596)


# --------------------------------------------------------------------------- #
# Aggregate: fanned per-sector keys inherit the session date
# --------------------------------------------------------------------------- #


def test_fanned_sector_keys_inherit_parent_date():
    domains = {
        "equities": {
            "dse_sector_heat": {
                "value": {"Banks": -0.08, "IT": -0.28},
                "cadence": "daily",
                "source_as_of": "2026-10-01",
                "_parse_strategy": "dse_sector_heat",
            },
        },
    }
    result = agg._build_source_as_of_map(domains)
    assert result["dse_sector_heat"] == date(2026, 10, 1)
    assert result["dse_sector_heat_banks"] == date(2026, 10, 1)
    assert result["dse_sector_heat_it"] == date(2026, 10, 1)

    data = {"dse_sector_heat": {"Banks": -0.08, "IT": -0.28}}
    agg._flatten_dict_indicators(data)
    minted = {k for k in data if k.startswith("dse_sector_heat_")}
    assert minted <= set(result)  # every minted key is dated


def test_undated_sector_heat_no_longer_silently_exempt():
    assert "dse_sector_heat" not in agg._NEVER_DATED_PARSE_STRATEGIES
