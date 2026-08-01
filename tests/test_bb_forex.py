"""Tests for scrapers/bb_forex.py.

All tests mock fetch_rendered_html to avoid live network calls.
Fixtures in tests/fixtures/ provide representative HTML snapshots.

CAPTCHA-helper tests live in tests/test_bb_forex_captcha.py.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from scrapers.bb_forex import (
    ParseError,
    _parse_reserves_date,
    load_previous_snapshot,
    parse_exchange_rates,
    parse_reserves,
    write_snapshot,
)
from utils.schema import ForexRates, ForexReserves, ForexSnapshot

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _make_snapshot(
    snapshot_date: date = date(2026, 4, 19),
    usd_mid: float = 122.7,
    usd_buy: float = 122.7,
    usd_sell: float = 122.7,
    eur_bdt: float = 144.34,
    gbp_bdt: float = 165.85,
    gross_reserves: float = 34.1166,
    reserves_date: date = date(2026, 3, 1),
) -> ForexSnapshot:
    rates = ForexRates(
        usd_bdt_mid=usd_mid,
        usd_bdt_buy=usd_buy,
        usd_bdt_sell=usd_sell,
        eur_bdt=eur_bdt,
        gbp_bdt=gbp_bdt,
        source_url="https://example.com/rates",
    )
    reserves = ForexReserves(
        gross_reserves_usd_bn=gross_reserves,
        import_cover_months=None,
        reserves_date=reserves_date,
        source_url="https://example.com/reserves",
    )
    return ForexSnapshot(
        schema_version="1.0",
        date=snapshot_date,
        scraped_at=datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc),
        rates=rates,
        reserves=reserves,
    )


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------


class TestParseExchangeRates:
    def test_returns_expected_fields(self):
        """All five float fields are present and positive when parsed from fixture."""
        html = _read_fixture("bb_exchange_rates.html")
        rates = parse_exchange_rates(html)

        assert isinstance(rates.usd_bdt_mid, float)
        assert isinstance(rates.usd_bdt_buy, float)
        assert isinstance(rates.usd_bdt_sell, float)
        assert isinstance(rates.eur_bdt, float)
        assert isinstance(rates.gbp_bdt, float)

        assert rates.usd_bdt_mid > 0
        assert rates.usd_bdt_buy > 0
        assert rates.usd_bdt_sell > 0
        assert rates.eur_bdt > 0
        assert rates.gbp_bdt > 0

    def test_usd_values_are_plausible(self):
        """USD/BDT rates should be in a realistic range (100-200)."""
        html = _read_fixture("bb_exchange_rates.html")
        rates = parse_exchange_rates(html)

        assert 100.0 < rates.usd_bdt_mid < 200.0
        assert 100.0 < rates.usd_bdt_buy < 200.0
        assert 100.0 < rates.usd_bdt_sell < 200.0

    def test_eur_gbp_are_mid_of_bid_ask(self):
        """EUR/GBP values are derived as mid = (bid + ask) / 2."""
        html = _read_fixture("bb_exchange_rates.html")
        # We can only verify the value is finite and positive; exact mid calculation
        # is tested via minimal HTML below.
        rates = parse_exchange_rates(html)
        assert rates.eur_bdt > rates.usd_bdt_mid  # EUR should be stronger than USD vs BDT

    def test_raises_on_missing_tables(self):
        """ParseError raised when HTML contains no tables in section.content."""
        html = "<html><body><section class='content'><p>no tables here</p></section></body></html>"
        with pytest.raises(ParseError, match="expected 2\\+ tables"):
            parse_exchange_rates(html)

    def test_raises_on_single_table(self):
        """ParseError raised when only one table is found (cross-rate table missing)."""
        html = (
            "<html><body><section class='content'>"
            "<table><tr><th>Currency</th><th>Bid</th><th>Ask</th><th>WAR</th></tr>"
            "<tr><td>USD</td><td>122.70</td><td>122.70</td><td>122.70</td></tr></table>"
            "</section></body></html>"
        )
        with pytest.raises(ParseError, match="expected 2\\+ tables"):
            parse_exchange_rates(html)

    def test_raises_when_usd_row_missing(self):
        """ParseError raised when USD row is absent from table 0."""
        html = (
            "<html><body><section class='content'>"
            "<table><tr><th>Currency</th><th>Bid</th><th>Ask</th><th>WAR</th></tr>"
            "<tr><td>GBP</td><td>165.0</td><td>165.1</td><td>165.05</td></tr></table>"
            "<table><tr><th>Currency</th><th>Bid</th><th>Ask</th></tr>"
            "<tr><td>EUR</td><td>144.0</td><td>144.1</td></tr>"
            "<tr><td>GBP</td><td>165.0</td><td>165.1</td></tr></table>"
            "</section></body></html>"
        )
        with pytest.raises(ParseError, match="Could not parse USD"):
            parse_exchange_rates(html)

    def test_mid_calculation_with_known_values(self):
        """EUR mid is exactly (bid + ask) / 2 with controlled input."""
        html = (
            "<html><body><section class='content'>"
            "<table><tr><th>Currency</th><th>Bid Rate</th><th>Ask Rate</th><th>WAR</th></tr>"
            "<tr><td>USD</td><td>122.50</td><td>122.90</td><td>122.70</td></tr></table>"
            "<table><tr><th>Currency</th><th>Bid Rate</th><th>Ask Rate</th></tr>"
            "<tr><td>EUR</td><td>140.00</td><td>142.00</td></tr>"
            "<tr><td>GBP</td><td>160.00</td><td>164.00</td></tr></table>"
            "</section></body></html>"
        )
        rates = parse_exchange_rates(html)
        assert rates.usd_bdt_mid == pytest.approx(122.70)
        assert rates.usd_bdt_buy == pytest.approx(122.50)
        assert rates.usd_bdt_sell == pytest.approx(122.90)
        assert rates.eur_bdt == pytest.approx(141.00)
        assert rates.gbp_bdt == pytest.approx(162.00)


class TestParseReserves:
    def test_converts_millions_to_billions(self):
        """Gross reserves are divided by 1000 to convert from millions to billions."""
        html = _read_fixture("bb_forex_reserves.html")
        reserves = parse_reserves(html)

        # Fixture shows March 2026 = 34116.6 million -> 34.1166 billion
        assert reserves.gross_reserves_usd_bn == pytest.approx(34.1166, abs=0.001)

    def test_import_cover_is_none(self):
        """import_cover_months is always None (not published on BB reserves page)."""
        html = _read_fixture("bb_forex_reserves.html")
        reserves = parse_reserves(html)
        assert reserves.import_cover_months is None

    def test_reserves_date_is_date_object(self):
        """reserves_date is a date object representing the first of the period month."""
        html = _read_fixture("bb_forex_reserves.html")
        reserves = parse_reserves(html)
        assert isinstance(reserves.reserves_date, date)
        assert reserves.reserves_date.day == 1

    def test_reserves_date_is_march_2026(self):
        """Most recent row in fixture is March 2026, driven off the fiscal-year
        header row ('2025-2026') actually present in the table — not off
        date.today(), so this holds regardless of which year the suite runs in."""
        html = _read_fixture("bb_forex_reserves.html")
        reserves = parse_reserves(html)
        assert reserves.reserves_date == date(2026, 3, 1)

    def test_raises_on_missing_table(self):
        """ParseError raised when #sortableTable is absent."""
        html = "<html><body><p>no table</p></body></html>"
        with pytest.raises(ParseError, match="sortableTable not found"):
            parse_reserves(html)

    def test_known_value_from_minimal_html(self):
        """mn->bn conversion verified with controlled HTML input."""
        html = (
            "<html><body>"
            "<table id='sortableTable'>"
            "<tr><td>(In million US $)</td></tr>"
            "<tr><td>Period</td><td>Foreign Exchange Reserves(Gross)</td><td>Foreign Exchange Reserves(as per BPM6)</td></tr>"
            "<tr><td>2025-2026</td></tr>"
            "<tr><td>March</td><td>34116.6</td><td>29501.2</td></tr>"
            "</table>"
            "</body></html>"
        )
        reserves = parse_reserves(html)
        assert reserves.gross_reserves_usd_bn == pytest.approx(34.1166, abs=0.0001)
        assert reserves.import_cover_months is None
        assert reserves.reserves_date == date(2026, 3, 1)


class TestParseReservesDate:
    """Direct coverage of _parse_reserves_date's fiscal-year math and the
    no-header fallback's future-date guard (landmine: the old fiscal-header
    regex was dead code, so every reserves_date silently used date.today().year)."""

    def test_fiscal_header_is_clock_independent(self):
        """A wildly wrong 'today' must not change a fiscal-header-driven result —
        proves the header path never consults the wall clock."""
        result = _parse_reserves_date("March", "2025-2026", today=date(1999, 1, 1))
        assert result == date(2026, 3, 1)

    def test_fiscal_window_second_half_month_maps_to_start_year(self):
        """November (second half of the fiscal year, Jul-Dec) under header
        '2026-2027' belongs to the start year, 2026."""
        result = _parse_reserves_date("November", "2026-2027")
        assert result == date(2026, 11, 1)

    def test_fiscal_window_first_half_month_maps_to_end_year(self):
        """February (first half of the fiscal year, Jan-Jun) under header
        '2026-2027' belongs to the end year, 2027."""
        result = _parse_reserves_date("February", "2026-2027")
        assert result == date(2027, 2, 1)

    def test_no_header_fallback_guards_the_january_window(self):
        """No fiscal header seen; today is 2027-01-05 and the row reads
        'November' — the naive fallback (today.year=2027) would land in the
        future, so it must roll back to 2026."""
        result = _parse_reserves_date("November", None, today=date(2027, 1, 5))
        assert result == date(2026, 11, 1)

    def test_no_header_fallback_january_stays_in_current_year(self):
        """Same wall-clock date, but the row itself reads 'January' — that
        does NOT land in the future, so no rollback is applied."""
        result = _parse_reserves_date("January", None, today=date(2027, 1, 5))
        assert result == date(2027, 1, 1)


# ---------------------------------------------------------------------------
# File I/O tests
# ---------------------------------------------------------------------------


class TestWriteSnapshotAtomic:
    def test_writes_json_file(self, tmp_path):
        """write_snapshot creates a JSON file at the expected path."""
        snapshot = _make_snapshot(snapshot_date=date(2026, 4, 20))

        with patch("scrapers.bb_forex.DATA_DIR", tmp_path):
            path = write_snapshot(snapshot)

        assert path.exists()
        assert path.suffix == ".json"
        assert path.stem == "2026-04-20"

    def test_no_tmp_file_after_write(self, tmp_path):
        """Atomic rename: no .tmp file left on disk after successful write."""
        snapshot = _make_snapshot(snapshot_date=date(2026, 4, 20))

        with patch("scrapers.bb_forex.DATA_DIR", tmp_path):
            write_snapshot(snapshot)

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_json_is_valid_snapshot(self, tmp_path):
        """Written JSON can be re-parsed into a valid ForexSnapshot."""
        snapshot = _make_snapshot(snapshot_date=date(2026, 4, 20))

        with patch("scrapers.bb_forex.DATA_DIR", tmp_path):
            path = write_snapshot(snapshot)

        raw = json.loads(path.read_text(encoding="utf-8"))
        recovered = ForexSnapshot.model_validate(raw)
        assert recovered.rates.usd_bdt_mid == snapshot.rates.usd_bdt_mid


class TestLoadPreviousSnapshot:
    def test_returns_none_when_no_data_dir(self, tmp_path):
        """Returns None when DATA_DIR does not exist."""
        missing_dir = tmp_path / "nonexistent"
        with patch("scrapers.bb_forex.DATA_DIR", missing_dir):
            result = load_previous_snapshot(date.today())
        assert result is None

    def test_returns_none_when_no_older_files(self, tmp_path):
        """Returns None when the only snapshot is today (not older)."""
        snapshot = _make_snapshot(snapshot_date=date.today())
        with patch("scrapers.bb_forex.DATA_DIR", tmp_path):
            write_snapshot(snapshot)
            result = load_previous_snapshot(date.today())
        assert result is None

    def test_loads_most_recent_older_snapshot(self, tmp_path):
        """Returns the snapshot with the latest date that is still before today."""
        old = _make_snapshot(snapshot_date=date(2026, 4, 18), usd_mid=121.0)
        older = _make_snapshot(snapshot_date=date(2026, 4, 17), usd_mid=120.0)

        with patch("scrapers.bb_forex.DATA_DIR", tmp_path):
            write_snapshot(older)
            write_snapshot(old)
            result = load_previous_snapshot(date(2026, 4, 20))

        assert result is not None
        assert result.rates.usd_bdt_mid == pytest.approx(121.0)


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------


RATES_HTML = _read_fixture("bb_exchange_rates.html")
RESERVES_HTML = _read_fixture("bb_forex_reserves.html")


@pytest.fixture
def mock_fetch():
    """Patch fetch_rendered_html to return fixture HTML for both URLs."""

    def side_effect(url: str, *args, **kwargs) -> str:
        if "exchangerate" in url:
            return RATES_HTML
        if "intreserve" in url:
            return RESERVES_HTML
        raise ValueError(f"Unexpected URL: {url}")

    with patch("scrapers.bb_forex.fetch_rendered_html", side_effect=side_effect) as m:
        yield m


class TestMain:
    def test_exit_0_on_success(self, mock_fetch, tmp_path):
        """main() returns 0 and writes a snapshot when fetch and parse succeed."""
        with (
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.load_previous_snapshot", return_value=None),
        ):
            from scrapers.bb_forex import main

            result = main()

        assert result == 0
        snapshots = list(tmp_path.glob("*.json"))
        assert len(snapshots) == 1

    def test_exit_1_on_fetch_failure(self, tmp_path):
        """main() returns 1 when fetch_rendered_html raises a generic (non-parse)
        exception — the generic `except Exception` branch, notified as a fetch
        failure."""
        with (
            patch("scrapers.bb_forex.fetch_rendered_html", side_effect=OSError("connection refused")),
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.notify") as mock_notify,
        ):
            from scrapers.bb_forex import main

            result = main()

        assert result == 1
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "error"
        assert call_args[1] == "bb_forex fetch failed"

    def test_exit_1_on_parse_error_notified_distinctly_from_fetch_failure(self, tmp_path):
        """A ParseError (e.g. BB's page layout changed) hits the dedicated
        `except ParseError` branch — notified as a parse failure, distinct from
        a generic fetch failure, with a hint that the layout may have changed."""
        with (
            patch("scrapers.bb_forex.fetch_rendered_html", return_value="<html></html>"),
            patch(
                "scrapers.bb_forex.parse_exchange_rates",
                side_effect=ParseError("expected 2+ tables, got 0"),
            ),
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.notify") as mock_notify,
        ):
            from scrapers.bb_forex import main

            result = main()

        assert result == 1
        assert list(tmp_path.glob("*.json")) == []
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "error"
        assert call_args[1] == "bb_forex parse failed"
        assert "layout may have changed" in call_args[2]

    def test_exit_2_on_rate_anomaly(self, mock_fetch, tmp_path):
        """main() returns 2 and skips write when USD rate exceeds threshold (>2%)."""
        # Previous snapshot has USD mid 10% lower — triggers anomaly
        prev_snapshot = _make_snapshot(
            snapshot_date=date(2026, 4, 19),
            usd_mid=111.5,   # live fixture will show ~122.70, >2% change
            usd_buy=111.5,
            usd_sell=111.5,
        )

        with (
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.load_previous_snapshot", return_value=prev_snapshot),
            patch("scrapers.bb_forex.notify") as mock_notify,
        ):
            from scrapers.bb_forex import main

            result = main()

        assert result == 2
        # Write must be skipped — no JSON files
        assert list(tmp_path.glob("*.json")) == []
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "warning"
        assert "anomaly" in call_args[1].lower()

    def test_exit_2_on_reserves_same_month_revision_anomaly(self, mock_fetch, tmp_path):
        """Same reserves_date as the freshly parsed reserves (March 2026, matching
        the live fixture) but gross reserves jumps >3% -- this is a same-month
        REVISION, not a month advance, so the existing fractional-change band
        still applies. It must be HELD (previous reserves value carried
        forward), NOT rejected outright -- the snapshot is still written with
        fresh rates."""
        prev_snapshot = _make_snapshot(
            snapshot_date=date(2026, 4, 19),
            usd_mid=122.7,
            usd_buy=122.7,
            usd_sell=122.7,
            eur_bdt=144.34,
            gbp_bdt=165.85,
            gross_reserves=50.0,  # far from live ~34.12bn -> > 3% change
            reserves_date=date(2026, 3, 1),  # SAME month as the live fixture's March 2026
        )

        with (
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.load_previous_snapshot", return_value=prev_snapshot),
            patch("scrapers.bb_forex.notify") as mock_notify,
        ):
            from scrapers.bb_forex import main

            result = main()

        assert result == 2
        # HOLD still writes -- the snapshot exists, with the OLD reserves value
        # carried and fresh rates.
        snapshots = list(tmp_path.glob("*.json"))
        assert len(snapshots) == 1
        raw = json.loads(snapshots[0].read_text(encoding="utf-8"))
        written = ForexSnapshot.model_validate(raw)
        assert written.reserves.gross_reserves_usd_bn == pytest.approx(50.0)
        assert written.reserves.reserves_date == date(2026, 3, 1)
        assert written.rates.usd_bdt_mid == pytest.approx(122.7000)

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "warning"
        assert "revision" in call_args[1].lower()

    def test_exit_0_on_reserves_month_advance_accepts_any_magnitude(self, mock_fetch, tmp_path):
        """Reserves month advances (May -> June) with an 8.77% step -- BB's real
        gross-reserves jump, 34.5478bn -> 37.5780bn -- must be ACCEPTED
        regardless of magnitude because it's an ordinary monthly publication
        advance, not daily noise."""
        prev_snapshot = _make_snapshot(
            snapshot_date=date(2026, 6, 5),
            usd_mid=122.7,
            usd_buy=122.7,
            usd_sell=122.7,
            eur_bdt=144.34,
            gbp_bdt=165.85,
            gross_reserves=34.5478,
            reserves_date=date(2026, 5, 1),
        )
        new_reserves = ForexReserves(
            gross_reserves_usd_bn=37.5780,
            import_cover_months=None,
            reserves_date=date(2026, 6, 1),
            source_url="https://example.com/reserves",
        )

        with (
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.load_previous_snapshot", return_value=prev_snapshot),
            patch("scrapers.bb_forex.parse_reserves", return_value=new_reserves),
            patch("scrapers.bb_forex.notify") as mock_notify,
        ):
            from scrapers.bb_forex import main

            result = main()

        assert result == 0
        snapshots = list(tmp_path.glob("*.json"))
        assert len(snapshots) == 1
        raw = json.loads(snapshots[0].read_text(encoding="utf-8"))
        written = ForexSnapshot.model_validate(raw)
        assert written.reserves.gross_reserves_usd_bn == pytest.approx(37.5780)
        assert written.reserves.reserves_date == date(2026, 6, 1)
        mock_notify.assert_not_called()

    def test_exit_2_on_reserves_month_regressed_held(self, mock_fetch, tmp_path):
        """New reserves_date is EARLIER than the previous snapshot's -- the
        wrong-column / layout-drift signature (e.g. accidentally reading the
        BPM6 column, which reads far lower than gross). HELD: the snapshot is
        still written but with the previous reserves value carried forward."""
        prev_snapshot = _make_snapshot(
            snapshot_date=date(2026, 4, 19),
            usd_mid=122.7,
            usd_buy=122.7,
            usd_sell=122.7,
            eur_bdt=144.34,
            gbp_bdt=165.85,
            gross_reserves=37.5780,
            reserves_date=date(2026, 6, 1),
        )
        regressed_reserves = ForexReserves(
            gross_reserves_usd_bn=32.9,  # BPM6-shaped lower figure
            import_cover_months=None,
            reserves_date=date(2026, 5, 1),  # earlier than prev's June
            source_url="https://example.com/reserves",
        )

        with (
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.load_previous_snapshot", return_value=prev_snapshot),
            patch("scrapers.bb_forex.parse_reserves", return_value=regressed_reserves),
            patch("scrapers.bb_forex.notify") as mock_notify,
        ):
            from scrapers.bb_forex import main

            result = main()

        assert result == 2
        snapshots = list(tmp_path.glob("*.json"))
        assert len(snapshots) == 1
        raw = json.loads(snapshots[0].read_text(encoding="utf-8"))
        written = ForexSnapshot.model_validate(raw)
        assert written.reserves.gross_reserves_usd_bn == pytest.approx(37.5780)
        assert written.reserves.reserves_date == date(2026, 6, 1)

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "warning"
        assert "regressed" in call_args[1].lower()

    def test_exit_0_when_prev_reserves_is_none(self, mock_fetch, tmp_path):
        """prev snapshot exists (so rate checks run) but its reserves is None
        -- e.g. the very first reserves read, or the day after a HOLD that had
        no baseline yet. Accepted unconditionally."""
        prev_snapshot = _make_snapshot(
            snapshot_date=date(2026, 4, 19),
            usd_mid=122.7,
            usd_buy=122.7,
            usd_sell=122.7,
            eur_bdt=144.34,
            gbp_bdt=165.85,
        )
        prev_no_reserves = prev_snapshot.model_copy(update={"reserves": None})

        with (
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.load_previous_snapshot", return_value=prev_no_reserves),
            patch("scrapers.bb_forex.notify") as mock_notify,
        ):
            from scrapers.bb_forex import main

            result = main()

        assert result == 0
        snapshots = list(tmp_path.glob("*.json"))
        assert len(snapshots) == 1
        raw = json.loads(snapshots[0].read_text(encoding="utf-8"))
        written = ForexSnapshot.model_validate(raw)
        assert written.reserves is not None
        assert written.reserves.gross_reserves_usd_bn == pytest.approx(34.1166)
        mock_notify.assert_not_called()

    def test_no_write_on_anomaly(self, mock_fetch, tmp_path):
        """Verify no partial JSON file exists after a RATE anomaly skip (the
        only case where the write is skipped entirely; a reserves anomaly
        instead HOLDS and still writes -- see test_exit_2_on_reserves_*)."""
        prev_snapshot = _make_snapshot(
            snapshot_date=date(2026, 4, 19),
            usd_mid=50.0,  # wildly wrong -> triggers a rate anomaly
        )

        with (
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.load_previous_snapshot", return_value=prev_snapshot),
            patch("scrapers.bb_forex.notify"),
        ):
            from scrapers.bb_forex import main

            result = main()

        assert result == 2
        assert list(tmp_path.glob("*.json")) == []
        assert list(tmp_path.glob("*.tmp")) == []

    def test_snapshot_values_are_sensible(self, mock_fetch, tmp_path):
        """Written snapshot has plausible USD and reserves values from live fixture."""
        with (
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.load_previous_snapshot", return_value=None),
        ):
            from scrapers.bb_forex import main

            main()

        snapshots = list(tmp_path.glob("*.json"))
        assert snapshots
        raw = json.loads(snapshots[0].read_text(encoding="utf-8"))
        snap = ForexSnapshot.model_validate(raw)

        assert 100.0 < snap.rates.usd_bdt_mid < 200.0
        assert 20.0 < snap.reserves.gross_reserves_usd_bn < 60.0
        assert snap.reserves.import_cover_months is None


# ---------------------------------------------------------------------------
# fetch_rendered_html retry behavior
# ---------------------------------------------------------------------------


class TestFetchRetry:
    """Verify fetch_rendered_html retries transient browser-level failures."""

    def test_succeeds_on_first_attempt(self):
        from scrapers.bb_forex import fetch_rendered_html

        with patch(
            "scrapers.bb_forex._fetch_once", return_value="<html>ok</html>"
        ) as m:
            out = fetch_rendered_html("https://example.com", max_attempts=3)
        assert out == "<html>ok</html>"
        assert m.call_count == 1

    def test_recovers_after_transient_failures(self):
        from scrapers.bb_forex import fetch_rendered_html

        side_effects = [
            RuntimeError("net::ERR_ADDRESS_UNREACHABLE"),
            RuntimeError("Page.goto: Timeout 60000ms exceeded"),
            "<html>ok</html>",
        ]
        with (
            patch(
                "scrapers.bb_forex._fetch_once", side_effect=side_effects
            ) as m,
            patch("scrapers.bb_forex.time.sleep") as mock_sleep,
        ):
            out = fetch_rendered_html("https://example.com", max_attempts=3)
        assert out == "<html>ok</html>"
        assert m.call_count == 3
        # 2 backoffs (5s, 10s) before the successful 3rd attempt
        assert mock_sleep.call_args_list == [((5,),), ((10,),)]

    def test_raises_last_error_after_exhausting_attempts(self):
        from scrapers.bb_forex import fetch_rendered_html

        side_effects = [
            RuntimeError("first"),
            RuntimeError("second"),
            RuntimeError("third — final"),
        ]
        with (
            patch(
                "scrapers.bb_forex._fetch_once", side_effect=side_effects
            ) as m,
            patch("scrapers.bb_forex.time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="third — final"):
                fetch_rendered_html("https://example.com", max_attempts=3)
        assert m.call_count == 3

    def test_no_sleep_after_final_failure(self):
        """Backoff must not run after the final attempt."""
        from scrapers.bb_forex import fetch_rendered_html

        with (
            patch(
                "scrapers.bb_forex._fetch_once",
                side_effect=[RuntimeError("a"), RuntimeError("b")],
            ),
            patch("scrapers.bb_forex.time.sleep") as mock_sleep,
        ):
            with pytest.raises(RuntimeError):
                fetch_rendered_html("https://example.com", max_attempts=2)
        # Only one backoff (5s) between attempts 1 and 2; nothing after attempt 2
        assert mock_sleep.call_args_list == [((5,),)]

    def test_passes_through_kwargs_to_fetch_once(self):
        from scrapers.bb_forex import fetch_rendered_html

        with patch(
            "scrapers.bb_forex._fetch_once", return_value="<html>ok</html>"
        ) as m:
            fetch_rendered_html(
                "https://example.com",
                timeout_ms=12345,
                wait_for_selector="div#x",
                max_attempts=1,
            )
        m.assert_called_once_with("https://example.com", 12345, "div#x")
