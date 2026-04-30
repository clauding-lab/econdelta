"""Unit tests for scrapers/commodity_prices.py."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scrapers.commodity_prices import (
    COMMODITY_SPEC,
    FetchError,
    fetch_commodity,
    load_previous_snapshot,
    main,
    write_snapshot,
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

def test_fetch_commodity_returns_price_and_prev_close():
    """fast_info dict path: returns (last_price, previous_close)."""
    # Arrange
    fi = _FastInfoDict({"last_price": 75.50, "previous_close": 74.20})
    mock_ticker = MagicMock()
    mock_ticker.fast_info = fi

    with patch("scrapers.commodity_prices.yf.Ticker", return_value=mock_ticker):
        # Act
        last, prev = fetch_commodity("BZ=F")

    # Assert
    assert last == pytest.approx(75.50)
    assert prev == pytest.approx(74.20)


def test_fetch_commodity_fallback_to_history_when_fast_info_missing():
    """When fast_info raises KeyError, falls back to history()."""
    # Arrange
    bad_fi = _FastInfoDict({})  # no last_price — __contains__ returns False
    close_series = pd.Series([70.0, 72.0, 74.0])
    mock_hist = pd.DataFrame({"Close": close_series})

    mock_ticker = MagicMock()
    mock_ticker.fast_info = bad_fi
    mock_ticker.history.return_value = mock_hist

    with patch("scrapers.commodity_prices.yf.Ticker", return_value=mock_ticker):
        # Act
        last, prev = fetch_commodity("BZ=F")

    # Assert
    assert last == pytest.approx(74.0)
    assert prev == pytest.approx(72.0)
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


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------

def _make_ticker_mock(last: float, prev: float) -> MagicMock:
    fi = _FastInfoDict({"last_price": last, "previous_close": prev})
    m = MagicMock()
    m.fast_info = fi
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
