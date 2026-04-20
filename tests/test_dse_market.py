"""Tests for scrapers/dse_market.py."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers.dse_market import ParseError, parse_homepage_indices, parse_market_stats
from utils.schema import DseSnapshot

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures: load captured HTML
# ---------------------------------------------------------------------------

@pytest.fixture()
def homepage_html() -> str:
    return (FIXTURES_DIR / "dse_homepage.html").read_text(encoding="utf-8")


@pytest.fixture()
def market_stats_html() -> str:
    return (FIXTURES_DIR / "dse_market_statistics.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit tests: parse_homepage_indices
# ---------------------------------------------------------------------------

class TestParseHomepageIndices:
    def test_parse_homepage_indices_returns_dsex_ds30_dses(self, homepage_html: str):
        """All three index values should be positive floats."""
        indices = parse_homepage_indices(homepage_html)

        assert isinstance(indices.dsex, float)
        assert isinstance(indices.ds30, float)
        assert isinstance(indices.dses, float)

        assert indices.dsex > 0
        assert indices.ds30 > 0
        assert indices.dses > 0

    def test_dsex_is_in_expected_ballpark(self, homepage_html: str):
        """DSEX should be near confirmed live value of 5232."""
        indices = parse_homepage_indices(homepage_html)
        assert 4000 < indices.dsex < 8000, f"DSEX {indices.dsex} outside expected range"

    def test_indices_include_change_fields(self, homepage_html: str):
        """dsex_change and dsex_change_pct must be present (can be negative)."""
        indices = parse_homepage_indices(homepage_html)
        assert isinstance(indices.dsex_change, float)
        assert isinstance(indices.dsex_change_pct, float)

    def test_raises_on_missing_left_col(self):
        """HTML without LeftColHome div should raise ParseError."""
        bad_html = "<html><body><div class='something_else'></div></body></html>"
        with pytest.raises(ParseError, match="LeftColHome"):
            parse_homepage_indices(bad_html)

    def test_raises_on_too_few_midrows(self):
        """Fewer than 3 midrow divs should raise ParseError."""
        bad_html = (
            "<html><body>"
            "<div class='LeftColHome'>"
            "<div class='midrow'><div class='m_col-1'>X</div><div class='m_col-2'>1</div>"
            "<div class='m_col-3'>0</div><div class='m_col-4'>0%</div></div>"
            "</div></body></html>"
        )
        with pytest.raises(ParseError, match="midrow"):
            parse_homepage_indices(bad_html)


# ---------------------------------------------------------------------------
# Unit tests: parse_market_stats
# ---------------------------------------------------------------------------

class TestParseMarketStats:
    def test_parse_market_stats_returns_expected_fields(self, market_stats_html: str):
        """All breadth and turnover fields must be present and positive."""
        market = parse_market_stats(market_stats_html)

        assert market.total_trades > 0
        assert market.turnover_crore > 0
        assert market.advancing > 0
        assert market.declining > 0
        assert market.unchanged >= 0

    def test_parse_market_stats_turnover_in_crore_not_taka(self, market_stats_html: str):
        """Turnover must be Taka divided by 10M (≈ 824 crore for confirmed live data)."""
        market = parse_market_stats(market_stats_html)

        # Confirmed live: 8247602308.40 Tk => ~824.76 crore
        # Accept a band of 500–2000 crore as sanity range
        assert 500 < market.turnover_crore < 2000, (
            f"turnover_crore {market.turnover_crore} looks like raw Taka (not divided by 10M)"
        )

    def test_turnover_conversion_exact(self, market_stats_html: str):
        """Verify the exact conversion: 8247602308.40 Tk => 824.7602 crore."""
        market = parse_market_stats(market_stats_html)
        expected_crore = 8_247_602_308.40 / 10_000_000
        assert abs(market.turnover_crore - expected_crore) < 0.01

    def test_advancing_declining_unchanged_match_confirmed_values(
        self, market_stats_html: str
    ):
        """Advancing=120, Declining=207, Unchanged=62 (confirmed live 2026-04-20)."""
        market = parse_market_stats(market_stats_html)
        assert market.advancing == 120
        assert market.declining == 207
        assert market.unchanged == 62

    def test_total_trades_matches_confirmed_value(self, market_stats_html: str):
        """Total trades = 223903 (confirmed live 2026-04-20)."""
        market = parse_market_stats(market_stats_html)
        assert market.total_trades == 223_903

    def test_parse_raises_on_missing_code_block(self):
        """HTML without a <code> element should raise ParseError."""
        bad_html = "<html><body><p>No code here</p></body></html>"
        with pytest.raises(ParseError, match="no <code> block"):
            parse_market_stats(bad_html)

    def test_parse_raises_when_trades_missing_from_code(self):
        """A <code> block missing the trades line should raise ParseError."""
        html_no_trades = (
            "<html><body><table><tr><td>"
            "<code>ISSUES ADVANCED : 100\nVALUE(Tk) : 1000000000.00</code>"
            "</td></tr></table></body></html>"
        )
        with pytest.raises(ParseError, match="NO. OF TRADES"):
            parse_market_stats(html_no_trades)


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
        date=date(2026, 4, 19),
        scraped_at=datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc),
        trading_day=trading_day,
        indices=indices,
        market=market,
        source_url="https://www.dse.com.bd/market-statistics.php",
    )
    return snap.model_dump(mode="json")


class TestMainEntryPoint:
    def test_non_trading_day_short_circuits(self, tmp_path, monkeypatch):
        """On a non-trading day: no fetch calls, trading_day=False snapshot, exit 0."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")

        # Redirect DATA_DIR to tmp
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        with (
            patch("scrapers.dse_market.is_bd_trading_day", return_value=False),
            patch("scrapers.dse_market.DEFAULT_CLIENT") as mock_client,
        ):
            from scrapers.dse_market import main

            result = main()

        assert result == 0
        mock_client.fetch_html.assert_not_called()

        # Verify the written snapshot has trading_day=False
        written_files = list(tmp_path.glob("*.json"))
        assert len(written_files) == 1
        data = json.loads(written_files[0].read_text())
        assert data["trading_day"] is False
        assert data["indices"] is None
        assert data["market"] is None

    def test_main_exit_1_on_fetch_failure(self, tmp_path, monkeypatch):
        """FetchError during fetch_html should return exit code 1."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        from utils.http_client import HttpClient

        with (
            patch("scrapers.dse_market.is_bd_trading_day", return_value=True),
            patch("scrapers.dse_market.load_holidays", return_value=set()),
            patch(
                "scrapers.dse_market.DEFAULT_CLIENT.fetch_html",
                side_effect=HttpClient.FetchError(
                    "https://www.dse.com.bd/", 503, "Service Unavailable"
                ),
            ),
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            from scrapers.dse_market import main

            result = main()

        assert result == 1
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "error"

    def test_main_exit_2_on_dsex_anomaly(self, tmp_path, monkeypatch):
        """DSEX 10% higher than previous snapshot should trigger anomaly exit 2."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        # Write previous snapshot with DSEX = 5000
        prev_data = _make_snapshot(trading_day=True, dsex=5000.0)
        prev_file = tmp_path / "2026-04-19.json"
        prev_file.write_text(json.dumps(prev_data))

        # New scraped DSEX = 5600 (12% jump — exceeds 10% default threshold)
        home_html = (FIXTURES_DIR / "dse_homepage.html").read_text(encoding="utf-8")
        stats_html = (FIXTURES_DIR / "dse_market_statistics.html").read_text(encoding="utf-8")

        # Parse real indices from fixture but inflate DSEX
        real_indices = parse_homepage_indices(home_html)
        inflated_dsex = 5000.0 * 1.12  # 12% jump

        from utils.schema import DseIndices

        mock_indices = DseIndices(
            dsex=inflated_dsex,
            dsex_change=real_indices.dsex_change,
            dsex_change_pct=real_indices.dsex_change_pct,
            ds30=real_indices.ds30,
            dses=real_indices.dses,
        )

        with (
            patch("scrapers.dse_market.is_bd_trading_day", return_value=True),
            patch("scrapers.dse_market.load_holidays", return_value=set()),
            patch("scrapers.dse_market.previous_trading_day", return_value=date(2026, 4, 19)),
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
            patch("scrapers.dse_market.parse_homepage_indices", return_value=mock_indices),
            patch(
                "scrapers.dse_market.parse_market_stats",
                return_value=parse_market_stats(stats_html),
            ),
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            mock_fetch.return_value = home_html

            from scrapers.dse_market import main

            result = main()

        assert result == 2
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "warning"
        assert "dsex" in call_args[2].lower()

        # Snapshot must NOT have been written
        written_files = [f for f in tmp_path.glob("*.json") if f.name != "2026-04-19.json"]
        assert len(written_files) == 0
