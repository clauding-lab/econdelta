"""Tests for scrapers/dse_market.py.

The fixture ``dse_api_live_market.json`` is a real, unmodified response from
``https://www.dse.com.bd/api/live/market`` captured 2026-10-01 ~19:39 UTC
(01:39 BDT 2 Oct), i.e. after the 1 Oct 2026 session closed -- the same
situation the 19:21 UTC systemd timer runs in.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from scrapers.dse_market import (
    ParseError,
    load_live_market,
    parse_indices,
    parse_live_market,
    parse_market,
    parse_session_date,
)
from utils.schema import DseSnapshot

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_SESSION = date(2026, 10, 1)
FIXTURE_PREV_SESSION = date(2026, 9, 30)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def live_market_text() -> str:
    return (FIXTURES_DIR / "dse_api_live_market.json").read_text(encoding="utf-8")


@pytest.fixture()
def live_market(live_market_text: str) -> dict:
    return json.loads(live_market_text)


# ---------------------------------------------------------------------------
# Unit tests: parse_indices
# ---------------------------------------------------------------------------

class TestParseIndices:
    def test_values_match_captured_response(self, live_market: dict):
        indices = parse_indices(live_market)
        assert indices.dsex == pytest.approx(5531.64623)
        assert indices.dsex_change == pytest.approx(-18.79079)
        assert indices.dsex_change_pct == pytest.approx(-0.33855)
        assert indices.ds30 == pytest.approx(2103.54659)
        assert indices.dses == pytest.approx(1100.25625)

    def test_matched_by_key_not_position(self, live_market: dict):
        live_market["indices"] = list(reversed(live_market["indices"]))
        indices = parse_indices(live_market)
        assert indices.dsex == pytest.approx(5531.64623)
        assert indices.ds30 == pytest.approx(2103.54659)

    def test_missing_dsex_raises(self, live_market: dict):
        live_market["indices"] = [i for i in live_market["indices"] if i["key"] != "DSEX"]
        with pytest.raises(ParseError, match="DSEX missing"):
            parse_indices(live_market)

    def test_non_numeric_value_raises(self, live_market: dict):
        live_market["indices"][0]["value"] = None
        with pytest.raises(ParseError, match="not numeric"):
            parse_indices(live_market)

    def test_missing_ds30_dses_are_none_not_fabricated(self, live_market: dict):
        live_market["indices"] = [i for i in live_market["indices"] if i["key"] == "DSEX"]
        indices = parse_indices(live_market)
        assert indices.ds30 is None and indices.dses is None


# ---------------------------------------------------------------------------
# Unit tests: parse_market
# ---------------------------------------------------------------------------

class TestParseMarket:
    def test_values_match_captured_response(self, live_market: dict):
        market = parse_market(live_market)
        assert market.total_trades == 184_783
        assert market.advancing == 85
        assert market.declining == 242
        assert market.unchanged == 58

    def test_turnover_tk_mn_converted_to_crore(self, live_market: dict):
        """totals.turnover is Tk MILLION (7231.11 mn = 723.111 crore)."""
        market = parse_market(live_market)
        assert market.turnover_crore == pytest.approx(723.111)
        # Same order of magnitude the old Tk-based page produced (~824 crore).
        assert 100 < market.turnover_crore < 5000

    def test_missing_breadth_raises(self, live_market: dict):
        del live_market["breadth"]
        with pytest.raises(ParseError, match="breadth"):
            parse_market(live_market)


# ---------------------------------------------------------------------------
# Unit tests: parse_session_date
# ---------------------------------------------------------------------------

class TestParseSessionDate:
    def test_uses_session_date_not_dhaka_today(self, live_market: dict):
        """session.date (2026-10-02, 'today in Dhaka') must be ignored in
        favour of session.sessionDate (2026-10-01, the session's own date)."""
        assert live_market["session"]["date"] == "2026-10-02"
        assert parse_session_date(live_market) == FIXTURE_SESSION

    def test_independent_of_run_date(self, live_market: dict, monkeypatch):
        class _FixedDate(date):
            @classmethod
            def today(cls):
                return date(2099, 1, 1)

        monkeypatch.setattr("scrapers.dse_market.date", _FixedDate)
        assert parse_session_date(live_market) == FIXTURE_SESSION

    @pytest.mark.parametrize(
        "is_open,phase",
        [(True, "open"), (False, "pre-open"), (False, "post-close"), (False, "halted")],
    )
    def test_refuses_non_closed_session(self, live_market: dict, is_open, phase):
        live_market["session"]["isOpen"] = is_open
        live_market["session"]["phase"] = phase
        with pytest.raises(ParseError, match="not closed"):
            parse_session_date(live_market)

    def test_missing_session_date_raises_never_falls_back(self, live_market: dict):
        del live_market["session"]["sessionDate"]
        with pytest.raises(ParseError, match="sessionDate"):
            parse_session_date(live_market)

    def test_malformed_session_date_raises(self, live_market: dict):
        live_market["session"]["sessionDate"] = "2026-13-40"
        with pytest.raises(ParseError, match="not a valid ISO date"):
            parse_session_date(live_market)

    def test_totals_disagreeing_with_daily_totals_raises(self, live_market: dict):
        live_market["totals"]["trades"] = 1
        with pytest.raises(ParseError, match="disagrees"):
            parse_session_date(live_market)

    def test_no_daily_totals_row_is_tolerated(self, live_market: dict):
        live_market["dailyTotals"] = []
        assert parse_session_date(live_market) == FIXTURE_SESSION


class TestLoadLiveMarket:
    def test_non_json_raises(self):
        with pytest.raises(ParseError, match="did not return JSON"):
            load_live_market("<html>Gone</html>")

    def test_non_object_raises(self):
        with pytest.raises(ParseError, match="not an object"):
            load_live_market("[1, 2]")

    def test_parse_live_market_roundtrip(self, live_market_text: str):
        d, indices, market = parse_live_market(live_market_text)
        assert d == FIXTURE_SESSION
        assert indices.dsex == pytest.approx(5531.64623)
        assert market.total_trades == 184_783


# ---------------------------------------------------------------------------
# Integration tests: main() entry point
# ---------------------------------------------------------------------------

def _make_snapshot(trading_day: bool = True, dsex: float = 5000.0) -> dict:
    from utils.schema import DseIndices, DseMarket

    indices = (
        DseIndices(
            dsex=dsex,
            dsex_change=-10.0,
            dsex_change_pct=-0.2,
            ds30=1900.0,
            dses=1000.0,
        )
        if trading_day
        else None
    )
    market = (
        DseMarket(
            turnover_crore=800.0,
            total_trades=200_000,
            advancing=100,
            declining=180,
            unchanged=50,
        )
        if trading_day
        else None
    )
    snap = DseSnapshot(
        schema_version="1.0",
        date=FIXTURE_PREV_SESSION,
        scraped_at=datetime(2026, 9, 30, 19, 21, 0, tzinfo=timezone.utc),
        trading_day=trading_day,
        indices=indices,
        market=market,
        source_url="https://www.dse.com.bd/api/live/market",
    )
    return snap.model_dump(mode="json")


def _inflated(live_market: dict, factor: float) -> str:
    """The real payload with every index level scaled by `factor`."""
    payload = copy.deepcopy(live_market)
    for row in payload["indices"]:
        row["value"] = row["value"] * factor
    return json.dumps(payload)


class TestMainEntryPoint:
    """main() fetches /api/live/market ONCE; every gate runs on the PARSED
    session date (2026-10-01 in the fixture), never date.today()."""

    def test_already_ingested_no_ops(self, tmp_path, monkeypatch, live_market_text):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        (tmp_path / "2026-10-01.json").write_text(json.dumps(_make_snapshot(dsex=5000.0)))

        with (
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            mock_fetch.side_effect = [live_market_text]

            from scrapers.dse_market import main

            result = main()

        assert result == 0
        assert mock_fetch.call_count == 1
        mock_notify.assert_not_called()
        assert len(list(tmp_path.glob("*.json"))) == 1

    def test_fetches_the_configured_api_url(self, tmp_path, monkeypatch, live_market_text):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        with patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch:
            mock_fetch.return_value = live_market_text

            from scrapers.dse_market import main

            assert main() == 0

        mock_fetch.assert_called_once_with("https://www.dse.com.bd/api/live/market")
        data = json.loads((tmp_path / "2026-10-01.json").read_text())
        assert data["source_url"] == "https://www.dse.com.bd/api/live/market"

    def test_main_exit_1_on_fetch_failure(self, tmp_path, monkeypatch):
        """FetchError (e.g. the 410 Gone the old URLs now return) -> exit 1."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        from utils.http_client import HttpClient

        with (
            patch(
                "scrapers.dse_market.DEFAULT_CLIENT.fetch_html",
                side_effect=HttpClient.FetchError(
                    "https://www.dse.com.bd/api/live/market", 410, "Gone"
                ),
            ),
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            from scrapers.dse_market import main

            result = main()

        assert result == 1
        mock_notify.assert_called_once()
        assert mock_notify.call_args[0][0] == "error"
        assert list(tmp_path.glob("*.json")) == []

    def test_main_exit_1_on_missing_session_date_never_falls_back(
        self, tmp_path, monkeypatch, live_market
    ):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)
        del live_market["session"]["sessionDate"]

        with (
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            mock_fetch.return_value = json.dumps(live_market)

            from scrapers.dse_market import main

            result = main()

        assert result == 1
        assert mock_notify.call_args[0][0] == "error"
        assert list(tmp_path.glob("*.json")) == []

    def test_main_exit_1_while_market_open_writes_nothing(
        self, tmp_path, monkeypatch, live_market
    ):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)
        live_market["session"].update({"isOpen": True, "phase": "open"})

        with (
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            mock_fetch.return_value = json.dumps(live_market)

            from scrapers.dse_market import main

            result = main()

        assert result == 1
        assert mock_notify.call_args[0][0] == "error"
        assert list(tmp_path.glob("*.json")) == []

    def test_writes_snapshot_dated_by_parsed_date_not_run_date(
        self, tmp_path, monkeypatch, live_market_text
    ):
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)
        monkeypatch.setattr("scrapers.dse_market.load_holidays", lambda _p: set())

        class _FixedDate(date):
            @classmethod
            def today(cls):
                return date(2099, 1, 1)

        monkeypatch.setattr("scrapers.dse_market.date", _FixedDate)

        with patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch:
            mock_fetch.return_value = live_market_text

            from scrapers.dse_market import main

            result = main()

        assert result == 0
        written = tmp_path / "2026-10-01.json"
        assert written.exists()
        data = json.loads(written.read_text())
        # Exact existing schema, unchanged.
        DseSnapshot.model_validate(data)
        assert data["date"] == "2026-10-01"
        assert data["trading_day"] is True
        assert data["indices"] == {
            "dsex": 5531.64623,
            "dsex_change": -18.79079,
            "dsex_change_pct": -0.33855,
            "ds30": 2103.54659,
            "dses": 1100.25625,
        }
        assert data["market"] == {
            "turnover_crore": 723.111,
            "total_trades": 184783,
            "advancing": 85,
            "declining": 242,
            "unchanged": 58,
        }

    def test_main_exit_2_on_dsex_anomaly(self, tmp_path, monkeypatch, live_market):
        """A 12% DSEX jump over a 1-day baseline gap blocks the write (exit 2)."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        prev = _make_snapshot(trading_day=True, dsex=5531.64623)
        (tmp_path / "2026-09-30.json").write_text(json.dumps(prev))

        with (
            patch("scrapers.dse_market.load_holidays", return_value=set()),
            patch("scrapers.dse_market.previous_trading_day", return_value=FIXTURE_PREV_SESSION),
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            mock_fetch.return_value = _inflated(live_market, 1.12)

            from scrapers.dse_market import main

            result = main()

        assert result == 2
        mock_notify.assert_called_once()
        assert mock_notify.call_args[0][0] == "warning"
        assert "dsex" in mock_notify.call_args[0][2].lower()
        assert not (tmp_path / "2026-10-01.json").exists()

    def test_anomaly_across_eid_window_gap_writes_with_warning_not_blocked(
        self, tmp_path, monkeypatch, live_market
    ):
        """MEDIUM-2: the same 12% move over a 7-day baseline gap writes + warns."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        prev = _make_snapshot(trading_day=True, dsex=5531.64623)
        prev["date"] = "2026-09-24"
        (tmp_path / "2026-09-24.json").write_text(json.dumps(prev))

        with (
            patch("scrapers.dse_market.load_holidays", return_value=set()),
            patch("scrapers.dse_market.previous_trading_day", return_value=date(2026, 9, 24)),
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            mock_fetch.return_value = _inflated(live_market, 1.12)

            from scrapers.dse_market import main

            result = main()

        assert result == 0
        mock_notify.assert_called_once()
        assert mock_notify.call_args[0][0] == "warning"
        assert "baseline gap" in mock_notify.call_args[0][1].lower()
        assert "dsex" in mock_notify.call_args[0][2].lower()
        assert (tmp_path / "2026-10-01.json").exists()

    def test_makeup_session_on_calendar_non_trading_day_still_writes(
        self, tmp_path, monkeypatch, live_market_text
    ):
        """A session the calendar calls non-trading (makeup Saturday) is still
        written -- see AGENT_LEARNINGS.md 2026-08-08."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        with (
            patch("scrapers.dse_market.is_bd_trading_day", return_value=False),
            patch("scrapers.dse_market.load_holidays", return_value=set()),
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
        ):
            mock_fetch.return_value = live_market_text

            from scrapers.dse_market import main

            result = main()

        assert result == 0
        assert (tmp_path / "2026-10-01.json").exists()
