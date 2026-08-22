"""Tests for scrapers/dse_market.py."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from scrapers.dse_market import (
    ParseError,
    parse_homepage_indices,
    parse_market_stats,
    parse_trading_date,
)
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
# Unit tests: parse_trading_date
# ---------------------------------------------------------------------------

class TestParseTradingDate:
    def test_extracts_iso_date_from_confirmed_fixture(self, market_stats_html: str):
        """The real fixture's own session date is 2026-04-20, not date.today()."""
        from scrapers.dse_market import _extract_code_block_text

        text = _extract_code_block_text(market_stats_html)
        assert parse_trading_date(text) == date(2026, 4, 20)

    def test_extracts_date_regardless_of_run_date(self, monkeypatch):
        """Never derives the date from date.today() -- patching it must not
        change the result at all."""
        text = "                  TODAY'S SHARE MARKET : 2026-07-09\n"

        class _FixedDate(date):
            @classmethod
            def today(cls):
                return date(2099, 1, 1)

        monkeypatch.setattr("scrapers.dse_market.date", _FixedDate)
        assert parse_trading_date(text) == date(2026, 7, 9)

    def test_raises_on_missing_label(self):
        """No 'TODAY'S SHARE MARKET' label at all -- must raise, never fall back."""
        with pytest.raises(ParseError, match="TODAY'S SHARE MARKET"):
            parse_trading_date("ISSUES ADVANCED : 100\nVALUE(Tk) : 1000000000.00")

    def test_raises_on_malformed_date_value(self):
        """A present-but-invalid ISO-shaped date (bad month/day) must raise,
        not silently default."""
        with pytest.raises(ParseError, match="not a valid ISO date"):
            parse_trading_date("TODAY'S SHARE MARKET : 2026-13-40")

    def test_accepts_curly_apostrophe_variant(self):
        """DSE's page may render a curly apostrophe (’) instead of a straight one."""
        text = "TODAY’S SHARE MARKET : 2026-04-20"
        assert parse_trading_date(text) == date(2026, 4, 20)


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
    """main()'s gate now runs AFTER fetch+parse and evaluates the PARSED trading
    date, never date.today() or a pre-fetch run-date check. The real fixtures
    (dse_market_statistics.html / dse_homepage.html) carry trading date
    2026-04-20, so DEFAULT_CLIENT.fetch_html is mocked with an ordered
    side_effect: [stats_html, homepage_html] (summary is fetched first)."""

    def test_already_ingested_no_ops_without_second_fetch(self, tmp_path, monkeypatch):
        """Parsed trading date already has a snapshot on disk -> no-op, exit 0,
        and the homepage is never fetched (only the stats page, to learn the date)."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        (tmp_path / "2026-04-20.json").write_text(json.dumps(_make_snapshot(dsex=5000.0)))

        stats_html = (FIXTURES_DIR / "dse_market_statistics.html").read_text(encoding="utf-8")

        with (
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            mock_fetch.side_effect = [stats_html]

            from scrapers.dse_market import main

            result = main()

        assert result == 0
        assert mock_fetch.call_count == 1  # stats only -- homepage never fetched
        mock_notify.assert_not_called()
        # No new file written, existing one untouched
        written_files = list(tmp_path.glob("*.json"))
        assert len(written_files) == 1

    def test_main_exit_1_on_fetch_failure(self, tmp_path, monkeypatch):
        """FetchError during the stats fetch should return exit code 1."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        from utils.http_client import HttpClient

        with (
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

    def test_main_exit_1_on_missing_trading_date_never_falls_back(self, tmp_path, monkeypatch):
        """market-statistics page with no 'TODAY'S SHARE MARKET' line must
        raise/exit 1 -- and critically, must NOT write a snapshot dated
        date.today() as a fallback."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        stats_html_no_date = (
            "<html><body><table><tr><td><code>\n"
            "A. NO. OF TRADES : 100\nC. VALUE(Tk) : 1000000000.00\n"
            "ISSUES ADVANCED : 10\nISSUES DECLINED : 5\nISSUES UNCHANGED : 2\n"
            "</code></td></tr></table></body></html>"
        )

        with (
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            mock_fetch.side_effect = [stats_html_no_date]

            from scrapers.dse_market import main

            result = main()

        assert result == 1
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "error"
        assert list(tmp_path.glob("*.json")) == []

    def test_writes_snapshot_dated_by_parsed_date_not_run_date(self, tmp_path, monkeypatch):
        """The written snapshot's `date` field is the PARSED trading date
        (2026-04-20, from the real fixture) even when date.today() is
        patched to a completely different day -- proving there is no
        run-date fallback anywhere in the write path."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)
        monkeypatch.setattr("scrapers.dse_market.load_holidays", lambda _p: set())

        class _FixedDate(date):
            @classmethod
            def today(cls):
                return date(2099, 1, 1)

        monkeypatch.setattr("scrapers.dse_market.date", _FixedDate)

        stats_html = (FIXTURES_DIR / "dse_market_statistics.html").read_text(encoding="utf-8")
        home_html = (FIXTURES_DIR / "dse_homepage.html").read_text(encoding="utf-8")

        with patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch:
            mock_fetch.side_effect = [stats_html, home_html]

            from scrapers.dse_market import main

            result = main()

        assert result == 0
        written = tmp_path / "2026-04-20.json"
        assert written.exists()
        data = json.loads(written.read_text())
        assert data["date"] == "2026-04-20"

    def test_main_exit_2_on_dsex_anomaly(self, tmp_path, monkeypatch):
        """DSEX 10% higher than previous snapshot should trigger anomaly exit 2."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        # Write previous snapshot with DSEX = 5000, dated the trading day
        # immediately before the fixture's own session date (2026-04-20).
        prev_data = _make_snapshot(trading_day=True, dsex=5000.0)
        prev_file = tmp_path / "2026-04-19.json"
        prev_file.write_text(json.dumps(prev_data))

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
            patch("scrapers.dse_market.load_holidays", return_value=set()),
            patch("scrapers.dse_market.previous_trading_day", return_value=date(2026, 4, 19)),
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
            patch("scrapers.dse_market.parse_homepage_indices", return_value=mock_indices),
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            mock_fetch.side_effect = [stats_html, home_html]

            from scrapers.dse_market import main

            result = main()

        assert result == 2
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "warning"
        assert "dsex" in call_args[2].lower()

        # No NEW snapshot written for the fixture's trading date
        assert not (tmp_path / "2026-04-20.json").exists()

    def test_anomaly_across_eid_window_gap_writes_with_warning_not_blocked(
        self, tmp_path, monkeypatch
    ):
        """MEDIUM-2 (2026-08-22 round-1 review): the SAME 12% DSEX move that
        hard-blocks the write on a normal 1-day gap must instead WRITE +
        warn when the baseline is a week old -- the config/holidays_2026.json
        completion in this PR means a 7-day Eid closure is now a real,
        expected baseline gap, and a week's worth of accumulated movement
        compressed into one comparison is not the anomaly this threshold
        exists to catch."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        # Previous session 7 calendar days before the fixture's own trading
        # date (2026-04-20) -- e.g. the last pre-Eid session.
        prev_data = _make_snapshot(trading_day=True, dsex=5000.0)
        prev_data["date"] = "2026-04-13"
        prev_file = tmp_path / "2026-04-13.json"
        prev_file.write_text(json.dumps(prev_data))

        home_html = (FIXTURES_DIR / "dse_homepage.html").read_text(encoding="utf-8")
        stats_html = (FIXTURES_DIR / "dse_market_statistics.html").read_text(encoding="utf-8")

        real_indices = parse_homepage_indices(home_html)
        inflated_dsex = 5000.0 * 1.12  # 12% jump -- same magnitude as the blocked test above

        from utils.schema import DseIndices

        mock_indices = DseIndices(
            dsex=inflated_dsex,
            dsex_change=real_indices.dsex_change,
            dsex_change_pct=real_indices.dsex_change_pct,
            ds30=real_indices.ds30,
            dses=real_indices.dses,
        )

        with (
            patch("scrapers.dse_market.load_holidays", return_value=set()),
            patch("scrapers.dse_market.previous_trading_day", return_value=date(2026, 4, 13)),
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
            patch("scrapers.dse_market.parse_homepage_indices", return_value=mock_indices),
            patch("scrapers.dse_market.notify") as mock_notify,
        ):
            mock_fetch.side_effect = [stats_html, home_html]

            from scrapers.dse_market import main

            result = main()

        assert result == 0
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "warning"
        assert "baseline gap" in call_args[1].lower()
        assert "dsex" in call_args[2].lower()

        # The snapshot for the new trading date WAS written this time.
        assert (tmp_path / "2026-04-20.json").exists()

    def test_makeup_session_on_calendar_non_trading_day_still_writes(self, tmp_path, monkeypatch):
        """A parsed trading date the calendar treats as non-trading (e.g. a
        DSE makeup Friday/Saturday session around Eid, or an uncalendared
        moon-sighting holiday) must still be WRITTEN, never silently
        dropped -- see AGENT_LEARNINGS.md 2026-08-08."""
        monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
        monkeypatch.setattr("scrapers.dse_market.DATA_DIR", tmp_path)

        stats_html = (FIXTURES_DIR / "dse_market_statistics.html").read_text(encoding="utf-8")
        home_html = (FIXTURES_DIR / "dse_homepage.html").read_text(encoding="utf-8")

        with (
            patch("scrapers.dse_market.is_bd_trading_day", return_value=False),
            patch("scrapers.dse_market.load_holidays", return_value=set()),
            patch("scrapers.dse_market.DEFAULT_CLIENT.fetch_html") as mock_fetch,
        ):
            mock_fetch.side_effect = [stats_html, home_html]

            from scrapers.dse_market import main

            result = main()

        assert result == 0
        # Written anyway -- the calendar's "non-trading" verdict never blocks
        # a genuinely new, parsed session.
        assert (tmp_path / "2026-04-20.json").exists()
