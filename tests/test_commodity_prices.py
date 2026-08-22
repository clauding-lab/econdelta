"""Unit tests for scrapers/commodity_prices.py."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scrapers.commodity_prices import (
    FetchError,
    fetch_commodity,
    main,
)
from utils.schema import CommodityPrice, CommoditySnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FastInfoDict:
    """Minimal dict-like stub that supports 'in' operator and __getitem__."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def get(self, key: str, default=None):
        return self._data.get(key, default)


def _make_snapshot(prices: dict[str, CommodityPrice]) -> CommoditySnapshot:
    return CommoditySnapshot(
        schema_version="1.0",
        date=date(2026, 4, 19),
        scraped_at=datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc),
        prices=prices,
        provider="yfinance",
    )


def _commodity_price(price: float, prev: float | None = None) -> CommodityPrice:
    change_pct = ((price - prev) / prev) if prev is not None and prev != 0 else None
    return CommodityPrice(
        price=price,
        prev_close=prev,
        change_pct=change_pct,
        currency="USD",
        unit="barrel",
    )


# ---------------------------------------------------------------------------
# fetch_commodity tests
# ---------------------------------------------------------------------------

def _history_df(dates: list[str], closes: list[float]) -> pd.DataFrame:
    """A history()-shaped DataFrame: a tz-AWARE DatetimeIndex localized to
    America/New_York (the real yfinance exchange timezone for BZ=F/CL=F/
    GC=F, all NYMEX/COMEX-listed) + a Close column -- never a bare naive or
    RangeIndex. MEDIUM-6 (2026-08-22 round-1 review): pinning this as the
    DEFAULT test fixture (not just one dedicated edge-case test) means any
    future refactor that naively normalizes to UTC before taking .date()
    fails broadly across this file, not just in one targeted test."""
    index = pd.to_datetime(dates).tz_localize("America/New_York")
    return pd.DataFrame({"Close": closes}, index=index)


def test_fetch_commodity_returns_price_and_prev_close():
    """fast_info dict path: returns (last_price, previous_close, quote_date)."""
    # Arrange
    fi = _FastInfoDict({"last_price": 75.50, "previous_close": 74.20})
    mock_ticker = MagicMock()
    mock_ticker.fast_info = fi
    mock_ticker.history.return_value = _history_df(
        ["2026-04-17", "2026-04-18", "2026-04-19"], [73.0, 74.20, 75.50]
    )

    with patch("scrapers.commodity_prices.yf.Ticker", return_value=mock_ticker):
        # Act
        last, prev, quote_date = fetch_commodity("BZ=F")

    # Assert
    assert last == pytest.approx(75.50)
    assert prev == pytest.approx(74.20)
    assert quote_date == date(2026, 4, 19)


def test_fetch_commodity_recovers_quote_date_even_via_fast_info_path():
    """The date always comes from history()'s index, even when fast_info
    supplied the price -- fast_info itself carries no date at all."""
    fi = _FastInfoDict({"last_price": 2300.0, "previous_close": 2290.0})
    mock_ticker = MagicMock()
    mock_ticker.fast_info = fi
    mock_ticker.history.return_value = _history_df(["2026-06-05"], [2300.0])

    with patch("scrapers.commodity_prices.yf.Ticker", return_value=mock_ticker):
        _, _, quote_date = fetch_commodity("GC=F")

    assert quote_date == date(2026, 6, 5)
    mock_ticker.history.assert_called_once_with(period="5d", auto_adjust=False)


def test_fetch_commodity_quote_date_none_when_history_unavailable():
    """history() raising -> quote_date is None, never date.today()
    (fast_info still supplies the price)."""
    fi = _FastInfoDict({"last_price": 75.50, "previous_close": 74.20})
    mock_ticker = MagicMock()
    mock_ticker.fast_info = fi
    mock_ticker.history.side_effect = RuntimeError("network blip")

    with patch("scrapers.commodity_prices.yf.Ticker", return_value=mock_ticker):
        last, prev, quote_date = fetch_commodity("BZ=F")

    assert last == pytest.approx(75.50)
    assert quote_date is None


def test_fetch_commodity_fallback_to_history_when_fast_info_missing():
    """When fast_info raises KeyError, falls back to history() for BOTH the
    price and the quote_date -- only ONE history() call is made either way."""
    # Arrange
    bad_fi = _FastInfoDict({})  # no last_price — __contains__ returns False
    mock_hist = _history_df(["2026-04-17", "2026-04-18", "2026-04-19"], [70.0, 72.0, 74.0])

    mock_ticker = MagicMock()
    mock_ticker.fast_info = bad_fi
    mock_ticker.history.return_value = mock_hist

    with patch("scrapers.commodity_prices.yf.Ticker", return_value=mock_ticker):
        # Act
        last, prev, quote_date = fetch_commodity("BZ=F")

    # Assert
    assert last == pytest.approx(74.0)
    assert prev == pytest.approx(72.0)
    assert quote_date == date(2026, 4, 19)
    mock_ticker.history.assert_called_once_with(period="5d", auto_adjust=False)


def test_fetch_commodity_raises_on_empty_history():
    """Empty history DataFrame -> FetchError raised."""
    # Arrange
    bad_fi = _FastInfoDict({})
    mock_ticker = MagicMock()
    mock_ticker.fast_info = bad_fi
    mock_ticker.history.return_value = pd.DataFrame({"Close": pd.Series([], dtype=float)})

    with patch("scrapers.commodity_prices.yf.Ticker", return_value=mock_ticker):
        # Act / Assert
        with pytest.raises(FetchError):
            fetch_commodity("BZ=F")


def test_quote_date_uses_exchange_local_calendar_day_not_utc():
    """MEDIUM-6 (2026-08-22 round-1 review): real yfinance history() rows
    carry a tz-AWARE America/New_York timestamp (NYMEX/COMEX exchange
    timezone). A future refactor that naively normalizes to UTC before
    taking .date() would shift the calendar date near the day boundary --
    pin the NY-local date explicitly with a timestamp chosen to differ
    under each interpretation, so that exact regression fails the suite."""
    from scrapers.commodity_prices import _quote_date_from_history

    # 2026-06-10 23:30 America/New_York (EDT, UTC-4) == 2026-06-11 03:30 UTC.
    ny_ts = pd.Timestamp("2026-06-10 23:30:00", tz="America/New_York")
    df = pd.DataFrame({"Close": [85.0]}, index=[ny_ts])

    assert _quote_date_from_history(df) == date(2026, 6, 10)
    # Sanity check on the test's own premise: the two interpretations really
    # do disagree for this timestamp.
    assert ny_ts.tz_convert("UTC").date() == date(2026, 6, 11)


def test_quote_date_from_history_defensive_on_non_datetime_index():
    """A non-datetime index (e.g. a bare RangeIndex) must degrade to None,
    never raise -- date recovery must not break price extraction."""
    from scrapers.commodity_prices import _quote_date_from_history

    bad_index_df = pd.DataFrame({"Close": [1.0, 2.0, 3.0]})  # default RangeIndex
    assert _quote_date_from_history(bad_index_df) is None
    assert _quote_date_from_history(None) is None
    assert _quote_date_from_history(pd.DataFrame({"Close": pd.Series([], dtype=float)})) is None


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------

def _make_ticker_mock(last: float, prev: float, quote_date: str | None = None) -> MagicMock:
    fi = _FastInfoDict({"last_price": last, "previous_close": prev})
    m = MagicMock()
    m.fast_info = fi
    if quote_date is not None:
        m.history.return_value = _history_df([quote_date], [last])
    else:
        # No quote_date supplied -> history() returns an empty frame, so
        # _quote_date_from_history degrades to None (matching "no date
        # available" behaviour) without needing every existing test to opt in.
        m.history.return_value = pd.DataFrame({"Close": pd.Series([], dtype=float)})
    return m


@patch("scrapers.commodity_prices.load_previous_snapshot", return_value=None)
@patch("scrapers.commodity_prices.write_snapshot")
@patch("scrapers.commodity_prices.notify")
@patch("scrapers.commodity_prices.yf.Ticker")
def test_main_writes_snapshot_with_all_commodities(
    mock_ticker_cls, mock_notify, mock_write, mock_prev
):
    """All 3 tickers succeed -> snapshot written with 3 prices, exit 0."""
    # Arrange: each ticker returns a distinct price
    prices = {
        "BZ=F": (85.0, 84.0),
        "CL=F": (80.0, 79.0),
        "GC=F": (2300.0, 2290.0),
    }

    def _side_effect(ticker_sym):
        last, prev = prices[ticker_sym]
        return _make_ticker_mock(last, prev)

    mock_ticker_cls.side_effect = _side_effect
    mock_write.return_value = Path("/fake/2026-04-20.json")

    # Act
    result = main()

    # Assert
    assert result == 0
    mock_write.assert_called_once()
    snapshot_arg: CommoditySnapshot = mock_write.call_args[0][0]
    assert len(snapshot_arg.prices) == 3
    assert "brent_crude" in snapshot_arg.prices
    assert "wti_crude" in snapshot_arg.prices
    assert "gold" in snapshot_arg.prices
    mock_notify.assert_not_called()


@patch("scrapers.commodity_prices.load_previous_snapshot", return_value=None)
@patch("scrapers.commodity_prices.write_snapshot")
@patch("scrapers.commodity_prices.notify")
@patch("scrapers.commodity_prices.yf.Ticker")
def test_main_stamps_snapshot_with_quote_date_not_run_date(
    mock_ticker_cls, mock_notify, mock_write, mock_prev, monkeypatch
):
    """snapshot.date is the yfinance QUOTE date (max across the 3 tickers),
    not date.today() -- proven by patching date.today() to a totally
    different day and confirming it has zero effect on the result."""
    prices = {
        "BZ=F": (85.0, 84.0, "2026-06-10"),
        "CL=F": (80.0, 79.0, "2026-06-10"),
        "GC=F": (2300.0, 2290.0, "2026-06-11"),  # gold quoted a day later
    }

    def _side_effect(ticker_sym):
        last, prev, qd = prices[ticker_sym]
        return _make_ticker_mock(last, prev, quote_date=qd)

    mock_ticker_cls.side_effect = _side_effect
    mock_write.return_value = Path("/fake/2026-06-11.json")

    class _FixedDate(date):
        @classmethod
        def today(cls):
            return date(2099, 1, 1)

    monkeypatch.setattr("scrapers.commodity_prices.date", _FixedDate)

    result = main()

    assert result == 0
    snapshot_arg: CommoditySnapshot = mock_write.call_args[0][0]
    # Latest of the three quote dates wins, never the (patched, wildly
    # different) run date.
    assert snapshot_arg.date == date(2026, 6, 11)


@patch("scrapers.commodity_prices.load_previous_snapshot", return_value=None)
@patch("scrapers.commodity_prices.write_snapshot")
@patch("scrapers.commodity_prices.notify")
@patch("scrapers.commodity_prices.yf.Ticker")
def test_main_falls_back_to_run_date_when_no_quote_date_available(
    mock_ticker_cls, mock_notify, mock_write, mock_prev, monkeypatch
):
    """When yfinance offers no quote date for ANY ticker this run, the
    snapshot degrades to date.today() -- the documented "source genuinely
    has no date" case, not a crash or a fabricated date."""
    prices = {"BZ=F": (85.0, 84.0), "CL=F": (80.0, 79.0), "GC=F": (2300.0, 2290.0)}

    def _side_effect(ticker_sym):
        last, prev = prices[ticker_sym]
        return _make_ticker_mock(last, prev)  # no quote_date -> empty history

    mock_ticker_cls.side_effect = _side_effect
    mock_write.return_value = Path("/fake/2026-06-11.json")

    class _FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 6, 11)

    monkeypatch.setattr("scrapers.commodity_prices.date", _FixedDate)

    result = main()

    assert result == 0
    snapshot_arg: CommoditySnapshot = mock_write.call_args[0][0]
    assert snapshot_arg.date == date(2026, 6, 11)


@patch("scrapers.commodity_prices.load_previous_snapshot", return_value=None)
@patch("scrapers.commodity_prices.write_snapshot")
@patch("scrapers.commodity_prices.notify")
@patch("scrapers.commodity_prices.yf.Ticker")
def test_main_partial_fetch_still_succeeds_with_warning(
    mock_ticker_cls, mock_notify, mock_write, mock_prev
):
    """1 of 3 tickers fails -> snapshot written with 2 prices, exit 0, warning fired."""
    # Arrange
    call_count = {"n": 0}

    def _side_effect(ticker_sym):
        call_count["n"] += 1
        if ticker_sym == "GC=F":
            # This ticker will fail
            bad = MagicMock()
            bad.fast_info = _FastInfoDict({})
            bad.history.return_value = pd.DataFrame({"Close": pd.Series([], dtype=float)})
            return bad
        return _make_ticker_mock(80.0, 79.0)

    mock_ticker_cls.side_effect = _side_effect
    mock_write.return_value = Path("/fake/2026-04-20.json")

    # Act
    result = main()

    # Assert
    assert result == 0
    mock_write.assert_called_once()
    snapshot_arg: CommoditySnapshot = mock_write.call_args[0][0]
    assert len(snapshot_arg.prices) == 2
    assert "gold" not in snapshot_arg.prices
    # Warning should fire for the failed ticker
    mock_notify.assert_called_once()
    call_args = mock_notify.call_args[0]
    assert call_args[0] == "warning"


@patch("scrapers.commodity_prices.load_previous_snapshot", return_value=None)
@patch("scrapers.commodity_prices.write_snapshot")
@patch("scrapers.commodity_prices.notify")
@patch("scrapers.commodity_prices.yf.Ticker")
def test_main_all_fetches_fail_exits_1(
    mock_ticker_cls, mock_notify, mock_write, mock_prev
):
    """All 3 tickers fail -> exit 1, error notification, no write."""
    # Arrange
    def _fail(_ticker_sym):
        bad = MagicMock()
        bad.fast_info = _FastInfoDict({})
        bad.history.return_value = pd.DataFrame({"Close": pd.Series([], dtype=float)})
        return bad

    mock_ticker_cls.side_effect = _fail

    # Act
    result = main()

    # Assert
    assert result == 1
    mock_write.assert_not_called()
    mock_notify.assert_called_once()
    call_args = mock_notify.call_args[0]
    assert call_args[0] == "error"


@patch("scrapers.commodity_prices.write_snapshot")
@patch("scrapers.commodity_prices.notify")
@patch("scrapers.commodity_prices.yf.Ticker")
def test_main_anomaly_skips_write(mock_ticker_cls, mock_notify, mock_write, tmp_path):
    """Brent at 60 vs prev 50 is 20% jump (threshold 8%) -> exit 2, no write."""
    # Arrange: previous snapshot with brent=50
    prev_prices = {
        "brent_crude": CommodityPrice(price=50.0, prev_close=49.0, change_pct=0.02, currency="USD", unit="barrel"),
        "wti_crude": CommodityPrice(price=78.0, prev_close=77.0, change_pct=0.013, currency="USD", unit="barrel"),
        "gold": CommodityPrice(price=2300.0, prev_close=2290.0, change_pct=0.004, currency="USD", unit="oz"),
    }
    prev_snapshot = _make_snapshot(prev_prices)

    # New fetch: brent=60 (20% up from 50 — well above 8% threshold)
    def _side_effect(ticker_sym):
        price_map = {
            "BZ=F": (60.0, 59.0),   # 20% jump from prev 50
            "CL=F": (79.0, 78.0),
            "GC=F": (2310.0, 2300.0),
        }
        last, prev = price_map[ticker_sym]
        return _make_ticker_mock(last, prev)

    mock_ticker_cls.side_effect = _side_effect

    with patch("scrapers.commodity_prices.load_previous_snapshot", return_value=prev_snapshot):
        # Act
        result = main()

    # Assert
    assert result == 2
    mock_write.assert_not_called()
    mock_notify.assert_called_once()
    call_args = mock_notify.call_args[0]
    assert call_args[0] == "warning"
    assert "brent_crude" in call_args[2]
