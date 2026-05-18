"""Tests for scrapers/bb_forex.py.

All tests mock fetch_rendered_html to avoid live network calls.
Fixtures in tests/fixtures/ provide representative HTML snapshots.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers.bb_forex import (
    ParseError,
    _extract_captcha_image,
    _is_captcha_page,
    _solve_captcha_via_claude,
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
        reserves_date=date(2026, 3, 1),
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
        """Most recent row in fixture is March 2026."""
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
        """main() returns 1 when fetch_rendered_html raises an exception."""
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

    def test_exit_2_on_reserves_anomaly(self, mock_fetch, tmp_path):
        """main() returns 2 when reserves change exceeds threshold (>3%)."""
        # Previous snapshot has reserves 10% higher than fixture (~34.12bn) -> anomaly
        prev_snapshot = _make_snapshot(
            snapshot_date=date(2026, 4, 19),
            usd_mid=122.7,
            usd_buy=122.7,
            usd_sell=122.7,
            eur_bdt=144.34,
            gbp_bdt=165.85,
            gross_reserves=50.0,  # far from live ~34.12bn -> > 3% change
        )

        with (
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.load_previous_snapshot", return_value=prev_snapshot),
            patch("scrapers.bb_forex.notify") as mock_notify,
        ):
            from scrapers.bb_forex import main

            result = main()

        assert result == 2
        assert list(tmp_path.glob("*.json")) == []
        mock_notify.assert_called_once()

    def test_no_write_on_anomaly(self, mock_fetch, tmp_path):
        """Verify no partial JSON file exists after anomaly skip."""
        prev_snapshot = _make_snapshot(
            snapshot_date=date(2026, 4, 19),
            usd_mid=50.0,  # wildly wrong -> triggers anomaly
        )

        with (
            patch("scrapers.bb_forex.DATA_DIR", tmp_path),
            patch("scrapers.bb_forex.load_previous_snapshot", return_value=prev_snapshot),
            patch("scrapers.bb_forex.notify"),
        ):
            from scrapers.bb_forex import main

            main()

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


def test_is_captcha_page_true_for_bb_captcha_fixture():
    html = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
    assert _is_captcha_page(html) is True


def test_is_captcha_page_false_for_normal_exchange_rates_fixture():
    # bb_forex_reserves.html already exists in fixtures dir (used by existing reserves tests)
    html = (FIXTURES_DIR / "bb_forex_reserves.html").read_text(encoding="utf-8")
    assert _is_captcha_page(html) is False


def test_is_captcha_page_false_for_partial_markers():
    # Has id="ans" alone but missing other markers
    html = '<html><body><input id="ans" /></body></html>'
    assert _is_captcha_page(html) is False


def test_extract_captcha_image_writes_decoded_png(tmp_path):
    html = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
    dest = tmp_path / "captcha.png"
    _extract_captcha_image(html, dest)
    assert dest.exists()
    assert dest.stat().st_size > 100
    # PNG magic bytes
    assert dest.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # Byte-identical to the saved fixture
    expected = (FIXTURES_DIR / "bb_forex_captcha.png").read_bytes()
    assert dest.read_bytes() == expected


def test_extract_captcha_image_raises_when_no_thumbnail(tmp_path):
    dest = tmp_path / "x.png"
    with pytest.raises(ParseError, match="no captcha image"):
        _extract_captcha_image("<html><body>nothing</body></html>", dest)


def _mock_completed_process(stdout: str, returncode: int = 0) -> MagicMock:
    p = MagicMock()
    p.stdout = stdout
    p.stderr = ""
    p.returncode = returncode
    return p


def test_solve_captcha_via_claude_returns_lowercase_word(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("Bottle\n")
        result = _solve_captcha_via_claude(img)
    assert result == "bottle"
    # confirm claude was invoked with the image attached
    argv = mock_run.call_args[0][0]
    assert "--print" in argv
    assert "--model" in argv
    assert any("claude-haiku" in a for a in argv)
    # @<path> must appear inside one of the argv elements (the prompt string)
    assert any(f"@{img}" in a for a in argv)


def test_solve_captcha_via_claude_strips_trailing_punctuation(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("arrows.\n")
        assert _solve_captcha_via_claude(img) == "arrows"


def test_solve_captcha_via_claude_returns_first_word_only(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("a red apple")
        result = _solve_captcha_via_claude(img)
    # Take first word, strip junk
    assert result == "a"


def test_solve_captcha_via_claude_returns_none_on_empty_output(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("\n")
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_returns_none_on_too_long(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("a" * 31)
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_returns_none_on_nonzero_exit(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("error", returncode=1)
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_returns_none_on_timeout(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_logs_stderr_and_elapsed_on_nonzero_exit(tmp_path, caplog):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mp = MagicMock()
        mp.stdout = ""
        mp.stderr = "claude: model claude-haiku-4-5 not available\n"
        mp.returncode = 1
        mock_run.return_value = mp
        with caplog.at_level(logging.WARNING, logger="bb_forex"):
            assert _solve_captcha_via_claude(img) is None
    # Must log the stderr snippet so first-failure diagnostics don't require SSH
    record_text = " ".join(r.getMessage() for r in caplog.records)
    assert "not available" in record_text
    # Must log elapsed time (seconds)
    assert "elapsed" in record_text.lower() or "s)" in record_text or "after" in record_text.lower()


# ---------------------------------------------------------------------------
# _solve_captcha_loop integration tests
# ---------------------------------------------------------------------------
#
# We test the captcha-handling logic in isolation via a fake page object,
# rather than mocking the full sync_playwright() context. This matches the
# existing TestFetchRetry pattern (patch at the smallest reasonable boundary).
#
# _solve_captcha_loop drives the captcha challenge using a passed-in page:
# read content, check for captcha markers, extract image, solve, fill #ans,
# click #jar, wait for navigation, re-check. Up to 3 attempts.


class _FakePage:
    """Minimal sync_playwright Page stub for captcha-loop tests.

    Pages return successive HTML payloads from `content_returns` on each
    page.content() call. fill/click record their calls for assertion;
    wait_for_load_state is a no-op.
    """

    def __init__(self, content_returns: list[str]) -> None:
        self._content_returns = list(content_returns)
        self.fill_calls: list[tuple[str, str]] = []
        self.click_calls: list[str] = []
        self.wait_for_load_state_calls: list[dict] = []

    def content(self) -> str:
        if not self._content_returns:
            # If a test drained the queue, repeat the last response.
            raise AssertionError("FakePage.content() called more times than queued")
        return self._content_returns.pop(0)

    def fill(self, selector: str, value: str) -> None:
        self.fill_calls.append((selector, value))

    def click(self, selector: str) -> None:
        self.click_calls.append(selector)

    def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        self.wait_for_load_state_calls.append({"state": state, "timeout": timeout})


_CAPTCHA_HTML = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
_RESERVES_HTML = (FIXTURES_DIR / "bb_forex_reserves.html").read_text(encoding="utf-8")


def test_solve_captcha_loop_returns_html_when_no_captcha_present():
    """Happy path — initial HTML is not a captcha page → return immediately, no fills/clicks."""
    from scrapers.bb_forex import _solve_captcha_loop

    page = _FakePage(content_returns=[])  # content() should not be called
    result = _solve_captcha_loop(page, _RESERVES_HTML, timeout_ms=60_000)

    assert result == _RESERVES_HTML
    assert page.fill_calls == []
    assert page.click_calls == []


def test_solve_captcha_loop_clears_captcha_on_first_solve():
    """First attempt: captcha detected → solver returns 'arrows' → page navigates → re-check shows non-captcha HTML."""
    from scrapers.bb_forex import _solve_captcha_loop

    # After click+navigation, page.content() returns the real HTML.
    page = _FakePage(content_returns=[_RESERVES_HTML])

    with patch(
        "scrapers.bb_forex._solve_captcha_via_claude", return_value="arrows"
    ) as mock_solve:
        result = _solve_captcha_loop(page, _CAPTCHA_HTML, timeout_ms=60_000)

    assert result == _RESERVES_HTML
    assert mock_solve.call_count == 1
    assert page.fill_calls == [("#ans", "arrows")]
    assert page.click_calls == ["#jar"]
    # wait_for_load_state was called after click
    assert len(page.wait_for_load_state_calls) == 1
    assert page.wait_for_load_state_calls[0]["state"] == "domcontentloaded"


def test_solve_captcha_loop_raises_after_3_failed_attempts():
    """Every re-check returns captcha HTML → ParseError after 3 attempts; solver called 3x."""
    from scrapers.bb_forex import _solve_captcha_loop

    # Each iteration re-reads page.content() after submit. All return captcha HTML.
    page = _FakePage(content_returns=[_CAPTCHA_HTML, _CAPTCHA_HTML, _CAPTCHA_HTML])

    with patch(
        "scrapers.bb_forex._solve_captcha_via_claude", return_value="wrong"
    ) as mock_solve:
        with pytest.raises(ParseError, match="captcha solve failed after 3 attempts"):
            _solve_captcha_loop(page, _CAPTCHA_HTML, timeout_ms=60_000)

    assert mock_solve.call_count == 3
    assert page.fill_calls == [("#ans", "wrong")] * 3
    assert page.click_calls == ["#jar"] * 3
