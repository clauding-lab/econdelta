"""Tests for the World Bank Pink Sheet scraper (scrapers/world_bank_pink_sheet.py).

All tests are fully local / NO egress: parse tests run against a small captured
.xlsx fixture (tests/fixtures/world_bank_pink_sheet_monthly.xlsx — a
structurally-faithful miniature of the real workbook: same 'Monthly Prices'
sheet, same YYYYMmm period keys, real Dec-2025 values, but the target commodities
sit in DIFFERENT columns than production to prove label-matching, landmine E).
Fetch/upsert tests mock the network and the Supabase writer. The real workbook IS
reachable from this Mac (NO BD egress wall — verified 2026-05-31, latest period
2025M12) but the unit tests stay offline so they pass deterministically (no CI).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers.world_bank_pink_sheet import (
    FetchError,
    fetch_pink_sheet_bytes,
    parse_pink_sheet,
    upsert_commodities,
)

FIXTURE = Path(__file__).parent / "fixtures" / "world_bank_pink_sheet_monthly.xlsx"


@pytest.fixture
def workbook_bytes() -> bytes:
    return FIXTURE.read_bytes()


# --------------------------------------------------------------------------- #
# parse_pink_sheet — pure (in-memory bytes), no egress
# --------------------------------------------------------------------------- #


def test_extracts_three_commodity_benchmarks(workbook_bytes):
    """LNG / Palm-oil / Wheat all resolve to their latest-period values."""
    values, _as_of = parse_pink_sheet(workbook_bytes)
    # Verified against the live World Bank workbook (Dec-2025) on 2026-05-31.
    assert values["lng_price_usd_mmbtu"] == pytest.approx(11.0607, rel=1e-4)
    assert values["palm_oil_price_usd_mt"] == pytest.approx(980.51, rel=1e-4)
    assert values["wheat_price_usd_mt"] == pytest.approx(223.1737, rel=1e-4)


def test_as_of_is_latest_period_month_end(workbook_bytes):
    """as_of is stamped at the month-end of the latest YYYYMmm period (2025M12)."""
    _values, as_of = parse_pink_sheet(workbook_bytes)
    assert as_of == date(2025, 12, 31)


def test_matches_by_label_not_by_column_index(workbook_bytes):
    """The fixture places targets in cols C/E/H/K (NOT production J/W/AK) — a
    fixed-index parse would grab the wrong cells; label-matching gets it right."""
    values, _as_of = parse_pink_sheet(workbook_bytes)
    # The fixture's column C is Brent crude (~$62/bbl) — must NOT leak into any target.
    assert all(v != pytest.approx(62.0) for v in values.values())
    assert values["lng_price_usd_mmbtu"] < 60.0  # $/mmbtu, far below any crude $/bbl


def test_picks_latest_period_not_an_earlier_row(workbook_bytes):
    """The fixture carries 2025M09..2025M12 — must take Dec, not an earlier month."""
    values, as_of = parse_pink_sheet(workbook_bytes)
    assert as_of == date(2025, 12, 31)
    # Nov palm-oil was 983.4; Dec is 980.51 — confirm we took Dec.
    assert values["palm_oil_price_usd_mt"] == pytest.approx(980.51, rel=1e-4)


def test_out_of_range_value_is_dropped():
    """A target whose latest value falls outside its band (column shift) is dropped,
    not stored — but other in-range targets still come through."""
    targets = {
        "Palm oil": ("palm_oil_price_usd_mt", (200.0, 3000.0)),
        # Force LNG's band absurdly tight so its real ~11 is rejected.
        "Liquefied natural gas, Japan": ("lng_price_usd_mmbtu", (1000.0, 2000.0)),
    }
    values, _as_of = parse_pink_sheet(FIXTURE.read_bytes(), targets=targets)
    assert "palm_oil_price_usd_mt" in values
    assert "lng_price_usd_mmbtu" not in values


def test_no_targets_resolve_raises():
    """If NO target label is found, raise (rather than upsert an empty payload)."""
    targets = {"Nonexistent Commodity, Mars": ("mars_price_usd_kg", (0.0, 1e9))}
    with pytest.raises(FetchError, match="no Pink Sheet commodity values"):
        parse_pink_sheet(FIXTURE.read_bytes(), targets=targets)


def test_missing_sheet_raises(workbook_bytes):
    with pytest.raises(FetchError, match="not found in workbook"):
        parse_pink_sheet(workbook_bytes, sheet_name="No Such Sheet")


# --------------------------------------------------------------------------- #
# fetch_pink_sheet_bytes — mocked network
# --------------------------------------------------------------------------- #


def test_fetch_returns_content_on_200():
    sess = MagicMock()
    sess.get.return_value = MagicMock(status_code=200, content=b"PK\x03\x04stub")
    assert fetch_pink_sheet_bytes(session=sess) == b"PK\x03\x04stub"


def test_fetch_raises_on_non_200():
    sess = MagicMock()
    sess.get.return_value = MagicMock(status_code=503)
    with pytest.raises(FetchError, match="HTTP 503"):
        fetch_pink_sheet_bytes(session=sess)


def test_fetch_forces_ipv4_before_the_get(monkeypatch):
    """R5: thedocs.worldbank.org's IPv6 is blackholed from the ExonVPS box and it
    resolves AAAA first, so the fetch must pin urllib3 to IPv4 (HAS_IPV6=False)
    BEFORE the GET — otherwise the connect stalls on the dead IPv6 address.
    Asserting the flag at sess.get call time (not just afterwards) proves the
    ordering. After the fetch the global must be RESTORED (bleed fix): the
    force_ipv4_only guard confines the override so it can't leak into the Supabase
    upsert later in this one-shot process. monkeypatch restores after the test."""
    import urllib3.util.connection as conn

    monkeypatch.setattr(conn, "HAS_IPV6", True)  # pretend a dual-stack start state

    def _assert_ipv4_then_respond(*_args, **_kwargs):
        assert conn.HAS_IPV6 is False, "IPv4 must be pinned before the GET fires"
        return MagicMock(status_code=200, content=b"PK\x03\x04stub")

    sess = MagicMock()
    sess.get.side_effect = _assert_ipv4_then_respond
    assert fetch_pink_sheet_bytes(session=sess) == b"PK\x03\x04stub"
    assert conn.HAS_IPV6 is True  # restored to the prior value — no bleed


# --------------------------------------------------------------------------- #
# upsert_commodities — mocked writer
# --------------------------------------------------------------------------- #


def test_upsert_writes_all_metrics_under_one_as_of():
    values = {
        "lng_price_usd_mmbtu": 11.06,
        "palm_oil_price_usd_mt": 980.51,
        "wheat_price_usd_mt": 223.17,
    }
    as_of = date(2025, 12, 31)
    with patch(
        "scrapers.world_bank_pink_sheet.upsert_metric_history", return_value=3
    ) as mock_up:
        written = upsert_commodities(values, as_of)
    assert written == 3
    _args, kwargs = mock_up.call_args
    assert kwargs["data"] == values
    assert kwargs["as_of"] == as_of
    assert kwargs["source"] == "World Bank Pink Sheet"
    # Each metric gets its OWN as_of in the map (all the same reporting month here).
    assert kwargs["source_as_of_map"] == {m: as_of for m in values}


def test_end_to_end_fixture_to_upsert(workbook_bytes):
    """Parse the fixture, then confirm exactly the 3 commodity ids upsert together."""
    values, as_of = parse_pink_sheet(workbook_bytes)
    with patch(
        "scrapers.world_bank_pink_sheet.upsert_metric_history", return_value=len(values)
    ) as mock_up:
        written = upsert_commodities(values, as_of)
    assert written == 3
    _args, kwargs = mock_up.call_args
    assert set(kwargs["data"].keys()) == {
        "lng_price_usd_mmbtu",
        "palm_oil_price_usd_mt",
        "wheat_price_usd_mt",
    }
