"""Tier-1 source_as_of fix — bb_forex / dse_market / commodity_prices never
went through the v3 registry's ``_build_source_as_of_map``, so their
``flatten_data`` keys could never receive a publication-date override: every
aggregate run stamped them with today's run date regardless of how stale the
underlying snapshot file actually was ("as_of forgery"). Concretely: BB's
MAY reserves figure (34.5478) kept re-stamping as today's date through
2026-08-01, and a Fri/Sat DSEX carry-forward (when the scraper failed to
write a fresh file) read as a fresh trading print.

This file covers:
  1. ``aggregate_latest._build_tier1_source_as_of_map`` unit tests (reserves
     month-end mapping, rates scraped_at date, DSE trading day, commodity
     scraped_at date, None-reserves skip, alias propagation).
  2. The load-bearing integration test — a frozen bb_forex snapshot run
     through the real ``main()`` write path must NOT have its Supabase rows
     stamped with today's date.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aggregate_latest as agg  # noqa: E402
from utils.schema import (  # noqa: E402
    CommodityPrice,
    CommoditySnapshot,
    DseIndices,
    DseMarket,
    DseSnapshot,
    ForexRates,
    ForexReserves,
    ForexSnapshot,
)

_NOW = datetime.now(timezone.utc)


def _forex_snapshot(
    scraped_at: datetime = _NOW,
    reserves_date: date = date(2026, 5, 1),
    gross_reserves_usd_bn: float = 34.5478,
) -> ForexSnapshot:
    return ForexSnapshot(
        date=date(2026, 4, 20),
        scraped_at=scraped_at,
        rates=ForexRates(
            usd_bdt_mid=122.7,
            usd_bdt_buy=122.7,
            usd_bdt_sell=122.7,
            eur_bdt=144.34,
            gbp_bdt=165.85,
            source_url="https://example.com",
        ),
        reserves=ForexReserves(
            gross_reserves_usd_bn=gross_reserves_usd_bn,
            import_cover_months=None,
            reserves_date=reserves_date,
            source_url="https://example.com",
        ),
    )


def _dse_snapshot(scraped_at: datetime = _NOW, trading_day_date: date = date(2026, 4, 20)) -> DseSnapshot:
    return DseSnapshot(
        date=trading_day_date,
        scraped_at=scraped_at,
        trading_day=True,
        indices=DseIndices(
            dsex=5232.49, dsex_change=-15.04, dsex_change_pct=-0.28, ds30=1980.0, dses=1059.7,
        ),
        market=DseMarket(
            turnover_crore=824.76, total_trades=223903, advancing=120, declining=207, unchanged=62,
        ),
        source_url="https://example.com",
    )


def _commodity_snapshot(scraped_at: datetime = _NOW) -> CommoditySnapshot:
    return CommoditySnapshot(
        date=date(2026, 4, 20),
        scraped_at=scraped_at,
        prices={
            "brent_crude": CommodityPrice(
                price=95.23, prev_close=90.38, change_pct=0.0537, currency="USD", unit="barrel",
            ),
        },
        provider="yfinance",
    )


def _write_snapshot(path: Path, snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit tests: _build_tier1_source_as_of_map
# ---------------------------------------------------------------------------


class TestReservesMonthEnd:
    def test_31_day_month_maps_to_last_day(self):
        forex = _forex_snapshot(reserves_date=date(2026, 5, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex})
        assert m["gross_reserves_usd_bn"] == date(2026, 5, 31)

    def test_leap_february_maps_to_29(self):
        forex = _forex_snapshot(reserves_date=date(2024, 2, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex})
        assert m["gross_reserves_usd_bn"] == date(2024, 2, 29)

    def test_non_leap_february_maps_to_28(self):
        forex = _forex_snapshot(reserves_date=date(2026, 2, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex})
        assert m["gross_reserves_usd_bn"] == date(2026, 2, 28)

    def test_none_reserves_skips_the_key(self):
        forex = _forex_snapshot().model_copy(update={"reserves": None})
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex})
        assert "gross_reserves_usd_bn" not in m
        assert "fx_reserve_gross_and_bpm6" not in m
        # rates still get an override -- only the reserves side is skipped
        assert "usd_bdt_mid" in m


class TestForexRatesScrapedAtDate:
    def test_rates_use_scraped_at_date(self):
        scraped = datetime(2026, 4, 1, 3, 0, tzinfo=timezone.utc)
        forex = _forex_snapshot(scraped_at=scraped)
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex})
        for key in ("usd_bdt_mid", "usd_bdt_buy", "usd_bdt_sell", "eur_bdt", "gbp_bdt"):
            assert m[key] == date(2026, 4, 1)

    def test_frozen_snapshot_re_read_keeps_its_own_old_scraped_at(self):
        """A snapshot scraped 20 days ago and never refreshed since must be
        dated by ITS scraped_at, not today's run date."""
        stale = datetime.now(timezone.utc) - timedelta(days=20)
        forex = _forex_snapshot(scraped_at=stale)
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex})
        assert m["usd_bdt_mid"] == stale.date()
        assert m["usd_bdt_mid"] != date.today()


class TestDseTradingDay:
    def test_dse_keys_use_the_snapshot_date_field_not_scraped_at(self):
        """DseSnapshot.date is the trading day itself (set by the scraper to
        date.today() only when is_bd_trading_day() was True); `trading_day`
        is a BOOL, not a date. Using scraped_at would be wrong on the
        Fri/Sat/holiday carry-forward path, where a snapshot re-read TODAY
        still describes the LAST trading day's close."""
        scraped_today = datetime.now(timezone.utc)
        dse = _dse_snapshot(scraped_at=scraped_today, trading_day_date=date(2026, 7, 16))
        m = agg._build_tier1_source_as_of_map({"dse_market": dse})
        for key in (
            "dsex", "dsex_change", "dsex_change_pct", "ds30", "dses",
            "turnover_crore", "total_trades", "advancing", "declining", "unchanged",
        ):
            assert m[key] == date(2026, 7, 16)

    def test_non_trading_day_with_no_indices_or_market_adds_no_keys(self):
        dse = DseSnapshot(
            date=date(2026, 7, 18), scraped_at=_NOW, trading_day=False,
            indices=None, market=None, source_url="https://example.com",
        )
        m = agg._build_tier1_source_as_of_map({"dse_market": dse})
        assert "dsex" not in m
        assert "turnover_crore" not in m


class TestCommodityScrapedAt:
    def test_commodity_keys_use_scraped_at_date(self):
        scraped = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
        commodities = _commodity_snapshot(scraped_at=scraped)
        m = agg._build_tier1_source_as_of_map({"commodity_prices": commodities})
        assert m["brent_crude_usd_barrel"] == date(2026, 4, 3)


class TestAliasPropagation:
    def test_usd_bdt_exchange_rate_inherits_usd_bdt_mid_date(self):
        forex = _forex_snapshot(scraped_at=datetime(2026, 4, 5, tzinfo=timezone.utc))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex})
        assert m["usd_bdt_exchange_rate"] == m["usd_bdt_mid"] == date(2026, 4, 5)

    def test_fx_reserve_gross_and_bpm6_inherits_reserves_month_end(self):
        forex = _forex_snapshot(reserves_date=date(2026, 6, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex})
        assert m["fx_reserve_gross_and_bpm6"] == m["gross_reserves_usd_bn"] == date(2026, 6, 30)


class TestEmptySnapshots:
    def test_all_none_returns_empty_map(self):
        m = agg._build_tier1_source_as_of_map(
            {"bb_forex": None, "dse_market": None, "commodity_prices": None}
        )
        assert m == {}


# ---------------------------------------------------------------------------
# Integration (load-bearing): the real write path must not forge as_of
# ---------------------------------------------------------------------------


def test_tier1_upsert_rows_carry_source_dates_not_todays_run_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FROZEN bb_forex (scraped 20 days ago, reserves_date=2026-05-01) run
    through the real aggregate write path, with fresh dse/commodity
    snapshots. The captured Supabase upsert rows must carry the SOURCE's own
    dates -- never today's run date.

    Pre-fix: `source_as_of_map` was built ONLY from the v3 registry's
    `domains` dict (`_build_source_as_of_map`), which bb_forex/dse_market/
    commodity_prices never populate -- so every Tier-1 key fell through to
    the writer's global `as_of=now.date()` fallback regardless of staleness.
    This test fails against that code and passes once
    `_build_tier1_source_as_of_map` is merged into the source_as_of_map the
    aggregate passes to `upsert_metric_history`.
    """
    stale_scrape = datetime.now(timezone.utc) - timedelta(days=20)
    forex = _forex_snapshot(
        scraped_at=stale_scrape, reserves_date=date(2026, 5, 1), gross_reserves_usd_bn=34.5478,
    )
    dse = _dse_snapshot(scraped_at=datetime.now(timezone.utc), trading_day_date=date(2026, 4, 20))
    commodities = _commodity_snapshot(scraped_at=datetime.now(timezone.utc))

    data_dir = tmp_path / "data"
    _write_snapshot(data_dir / "bb_forex" / "2026-04-20.json", forex)
    _write_snapshot(data_dir / "dse_market" / "2026-04-20.json", dse)
    _write_snapshot(data_dir / "commodity_prices" / "2026-04-20.json", commodities)

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "sources.json"
    cfg_path.write_text(
        json.dumps({"sources": {
            "bb_exchange_rates": {"url": "https://example.com/forex"},
            "dse_market_summary": {"url": "https://example.com/dse"},
        }}),
        encoding="utf-8",
    )

    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", data_dir / "latest.json")
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    # Exercise the real Supabase write branch (conftest skips it by default).
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")

    import utils.supabase_writer as sw

    captured: dict = {}

    def _fake_upsert(**kwargs):
        captured.update(kwargs)
        return len(kwargs.get("data", {}))

    monkeypatch.setattr(sw, "upsert_metric_history", _fake_upsert)
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **k: 0)

    exit_code = agg.main()
    assert exit_code == 0
    assert captured, "expected upsert_metric_history to be called"

    rows = sw._rows_from_data(
        captured["data"], captured["as_of"], "EconDelta",
        captured.get("source_as_of_map"), captured.get("ingested_at"),
    )
    by_id = {r["metric_id"]: r["as_of"] for r in rows}

    today_iso = datetime.now(timezone.utc).date().isoformat()

    assert by_id["gross_reserves_usd_bn"] == "2026-05-31"
    assert by_id["usd_bdt_mid"] == stale_scrape.date().isoformat()
    assert by_id["dsex"] == "2026-04-20"
    assert by_id["brent_crude_usd_barrel"] == datetime.now(timezone.utc).date().isoformat()

    for mid in ("gross_reserves_usd_bn", "usd_bdt_mid"):
        assert by_id[mid] != today_iso, f"{mid} must not be stamped with today's run date"
