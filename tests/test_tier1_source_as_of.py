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
     month-end mapping, rates/commodity snapshot-date field, DSE trading day,
     None-reserves skip, alias propagation, the bb_forex_ok gate).
  2. The load-bearing integration test — a frozen bb_forex snapshot run
     through the real ``main()`` write path must NOT have its Supabase rows
     stamped with today's date.

Review round 2 note: `bb_forex_ok` is a REQUIRED keyword-only argument on
`_build_tier1_source_as_of_map` (no default) — every call below passes it
explicitly, even in tests where its value doesn't affect the assertion, so a
future signature regression (e.g. someone re-adding a default) can't hide.
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
    snapshot_date: date | None = None,
) -> ForexSnapshot:
    """`date` defaults to `scraped_at.date()` -- realistic pairing, since the
    real scraper sets both fields at the same moment (scrapers/bb_forex.py:
    ``date=date.today(), scraped_at=datetime.now(timezone.utc)``). Pass
    `snapshot_date` explicitly to decouple them (used to prove the Tier-1 map
    reads `.date`, not `.scraped_at`)."""
    return ForexSnapshot(
        date=snapshot_date if snapshot_date is not None else scraped_at.date(),
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


def _commodity_snapshot(
    scraped_at: datetime = _NOW, snapshot_date: date | None = None
) -> CommoditySnapshot:
    """`date` defaults to `scraped_at.date()` -- realistic pairing, mirroring
    `_forex_snapshot` (scrapers/commodity_prices.py sets both fields at the
    same moment: ``date=date.today(), scraped_at=datetime.now(timezone.utc)``).
    Pass `snapshot_date` explicitly to decouple them."""
    return CommoditySnapshot(
        date=snapshot_date if snapshot_date is not None else scraped_at.date(),
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
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=False)
        assert m["gross_reserves_usd_bn"] == date(2026, 5, 31)

    def test_leap_february_maps_to_29(self):
        forex = _forex_snapshot(reserves_date=date(2024, 2, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=False)
        assert m["gross_reserves_usd_bn"] == date(2024, 2, 29)

    def test_non_leap_february_maps_to_28(self):
        forex = _forex_snapshot(reserves_date=date(2026, 2, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=False)
        assert m["gross_reserves_usd_bn"] == date(2026, 2, 28)

    def test_none_reserves_skips_the_key(self):
        forex = _forex_snapshot().model_copy(update={"reserves": None})
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=True)
        assert "gross_reserves_usd_bn" not in m
        assert "fx_reserve_gross_and_bpm6" not in m
        assert "import_cover_months" not in m
        # rates still get an override -- only the reserves side is skipped
        assert "usd_bdt_mid" in m


class TestImportCoverMonths:
    """Review round 1, item 3: import_cover_months is flatten_data's other
    reserves-block key (set unconditionally alongside gross_reserves_usd_bn),
    but it was missing from the Tier-1 map entirely -- reviewer proved it
    landed as_of=today while its sibling gross_reserves_usd_bn got the honest
    month-end date."""

    def test_import_cover_months_maps_to_reserves_month_end(self):
        forex = _forex_snapshot(reserves_date=date(2026, 5, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=False)
        assert m["import_cover_months"] == date(2026, 5, 31) == m["gross_reserves_usd_bn"]

    def test_import_cover_months_not_gated_on_bb_forex_ok(self):
        """Unlike the two force-overwrite alias keys, import_cover_months is
        set unconditionally by flatten_data (not part of the freshness-gated
        block) -- it must get a date regardless of bb_forex_ok."""
        forex = _forex_snapshot(reserves_date=date(2026, 5, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=False)
        assert m["import_cover_months"] == date(2026, 5, 31)


class TestForexRatesSnapshotDate:
    """Review round 1, item 2: rates (and their alias) are dated from
    `forex.date` (the scraper's own calendar-day field), not
    `forex.scraped_at.date()` (a UTC timestamp). Both regimes agree under the
    current 00:0x-UTC retry-writer pattern, but `forex.date` stays correct if
    the primary ~23:05 UTC slot ever survives on the BDT-local box --
    scraped_at's UTC calendar date would then lag the intended BDT reporting
    day by one, shaving the Monday briefing's zero margin negative."""

    def test_rates_use_the_date_field(self):
        forex = _forex_snapshot(snapshot_date=date(2026, 4, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=False)
        for key in ("usd_bdt_mid", "usd_bdt_buy", "usd_bdt_sell", "eur_bdt", "gbp_bdt"):
            assert m[key] == date(2026, 4, 1)

    def test_uses_date_field_not_scraped_at_when_they_differ(self):
        """The decoupling proof: scraped_at says one UTC day, `date` says
        another -- the map must follow `date`."""
        scraped_at_utc = datetime(2026, 4, 1, 23, 5, tzinfo=timezone.utc)
        forex = _forex_snapshot(scraped_at=scraped_at_utc, snapshot_date=date(2026, 4, 2))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=False)
        assert m["usd_bdt_mid"] == date(2026, 4, 2)
        assert m["usd_bdt_mid"] != scraped_at_utc.date()

    def test_frozen_snapshot_re_read_keeps_its_own_old_date(self):
        """A snapshot written 20 days ago and never refreshed since must be
        dated by ITS OWN `date` field (realistic pairing: scraped_at and date
        set together at write time), not today's run date."""
        stale = datetime.now(timezone.utc) - timedelta(days=20)
        forex = _forex_snapshot(scraped_at=stale)  # date defaults to stale.date()
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=False)
        assert m["usd_bdt_mid"] == stale.date()
        assert m["usd_bdt_mid"] != date.today()


class TestDseTradingDay:
    def test_dse_keys_use_the_snapshot_date_field_not_scraped_at(self):
        """DseSnapshot.date is set to date.today() on EVERY scraper run,
        trading day or not (scrapers/dse_market.py:227-236 sets it on the
        non-trading path too -- it is NOT "only on trading days"). What
        distinguishes a trading day is `indices`/`market` being populated
        (a bool, `trading_day`, tracks that separately). Using scraped_at
        would be wrong on the Fri/Sat/holiday/failed-scrape carry-forward
        path, where a snapshot file re-read TODAY still honestly describes
        the LAST trading day's close via its own `date` field."""
        scraped_today = datetime.now(timezone.utc)
        dse = _dse_snapshot(scraped_at=scraped_today, trading_day_date=date(2026, 7, 16))
        m = agg._build_tier1_source_as_of_map({"dse_market": dse}, bb_forex_ok=False)
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
        m = agg._build_tier1_source_as_of_map({"dse_market": dse}, bb_forex_ok=False)
        assert "dsex" not in m
        assert "turnover_crore" not in m


class TestCommoditySnapshotDate:
    """Review round 2, item 2: commodities are dated from `commodities.date`
    (the scraper's own calendar-day field), not `commodities.scraped_at.date()`
    (a UTC timestamp) -- the commodity timer fires ~23:08 UTC, the same
    pre-midnight off-by-one risk that justified the forex change (item 2 of
    review round 1)."""

    def test_commodity_keys_use_the_date_field(self):
        commodities = _commodity_snapshot(snapshot_date=date(2026, 4, 3))
        m = agg._build_tier1_source_as_of_map({"commodity_prices": commodities}, bb_forex_ok=False)
        assert m["brent_crude_usd_barrel"] == date(2026, 4, 3)

    def test_uses_date_field_not_scraped_at_when_they_differ(self):
        """Decoupled fixture: scraped_at 23:08Z day N, date day N+1 (matching
        the commodity timer's actual near-midnight-UTC fire time) -- `.date`
        must win, not `.scraped_at.date()`."""
        scraped_at_utc = datetime(2026, 4, 1, 23, 8, tzinfo=timezone.utc)
        commodities = _commodity_snapshot(scraped_at=scraped_at_utc, snapshot_date=date(2026, 4, 2))
        m = agg._build_tier1_source_as_of_map({"commodity_prices": commodities}, bb_forex_ok=False)
        assert m["brent_crude_usd_barrel"] == date(2026, 4, 2)
        assert m["brent_crude_usd_barrel"] != scraped_at_utc.date()


class TestAliasPropagation:
    """Review round 1, item 1 (HIGH): the two force-overwrite alias keys
    (usd_bdt_exchange_rate, fx_reserve_gross_and_bpm6) get their VALUE from
    bb_forex only when bb_forex's status is "ok" (aggregate_latest.main(),
    the freshness gate). The DATE override must follow that SAME gate --
    otherwise a stale bb_forex can date a fresh v3-sourced value with its own
    stale date (proven live: value 999.9 from v3, date from 48h-old bb_forex)."""

    def test_usd_bdt_exchange_rate_inherits_usd_bdt_mid_date_when_bb_forex_ok(self):
        forex = _forex_snapshot(snapshot_date=date(2026, 4, 5))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=True)
        assert m["usd_bdt_exchange_rate"] == m["usd_bdt_mid"] == date(2026, 4, 5)

    def test_fx_reserve_gross_and_bpm6_inherits_reserves_month_end_when_bb_forex_ok(self):
        forex = _forex_snapshot(reserves_date=date(2026, 6, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=True)
        assert m["fx_reserve_gross_and_bpm6"] == m["gross_reserves_usd_bn"] == date(2026, 6, 30)

    def test_alias_keys_get_no_tier1_date_when_bb_forex_not_ok(self):
        """The date must follow the (gated) value: when bb_forex is stale,
        no Tier-1 date is stamped on either alias key at all -- the raw
        usd_bdt_mid/gross_reserves_usd_bn keys are unaffected (not gated)."""
        forex = _forex_snapshot(reserves_date=date(2026, 6, 1))
        m = agg._build_tier1_source_as_of_map({"bb_forex": forex}, bb_forex_ok=False)
        assert "usd_bdt_exchange_rate" not in m
        assert "fx_reserve_gross_and_bpm6" not in m
        assert "usd_bdt_mid" in m
        assert "gross_reserves_usd_bn" in m

    def test_bb_forex_ok_is_a_required_keyword_argument(self):
        """Review round 2, item 3: bb_forex_ok has no default -- a caller
        that forgets to pass it gets a TypeError at the call site, not a
        silently-wrong "no alias date" fallback (which is what a permissive
        default would produce, indistinguishable from a correctly-computed
        False)."""
        forex = _forex_snapshot()
        with pytest.raises(TypeError):
            agg._build_tier1_source_as_of_map({"bb_forex": forex})  # missing bb_forex_ok


class TestEmptySnapshots:
    def test_all_none_returns_empty_map(self):
        m = agg._build_tier1_source_as_of_map(
            {"bb_forex": None, "dse_market": None, "commodity_prices": None},
            bb_forex_ok=False,
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
