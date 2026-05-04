"""Unit + integration tests for aggregate_latest.py."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make sure repo root is on sys.path so `import aggregate_latest` works
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aggregate_latest as agg
from utils.schema import (
    CommodityPrice,
    CommoditySnapshot,
    DseIndices,
    DseMarket,
    DseSnapshot,
    ForexRates,
    ForexReserves,
    ForexSnapshot,
    LatestBundle,
    SourceStatus,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)


def _forex_snapshot(scraped_at: datetime = _NOW) -> ForexSnapshot:
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
            gross_reserves_usd_bn=34.1166,
            import_cover_months=None,
            reserves_date=date(2026, 3, 1),
            source_url="https://example.com",
        ),
    )


def _dse_snapshot(
    scraped_at: datetime = _NOW,
    trading_day: bool = True,
) -> DseSnapshot:
    if trading_day:
        indices = DseIndices(
            dsex=5232.49,
            dsex_change=-15.04,
            dsex_change_pct=-0.28,
            ds30=1980.0,
            dses=1059.7,
        )
        market = DseMarket(
            turnover_crore=824.76,
            total_trades=223903,
            advancing=120,
            declining=207,
            unchanged=62,
        )
    else:
        indices = None
        market = None
    return DseSnapshot(
        date=date(2026, 4, 20),
        scraped_at=scraped_at,
        trading_day=trading_day,
        indices=indices,
        market=market,
        source_url="https://example.com",
    )


def _commodity_snapshot(scraped_at: datetime = _NOW) -> CommoditySnapshot:
    return CommoditySnapshot(
        date=date(2026, 4, 20),
        scraped_at=scraped_at,
        prices={
            "brent_crude": CommodityPrice(
                price=95.23,
                prev_close=90.38,
                change_pct=0.0537,
                currency="USD",
                unit="barrel",
            ),
            "gold": CommodityPrice(
                price=4820.9,
                prev_close=4813.3,
                change_pct=0.0016,
                currency="USD",
                unit="oz",
            ),
        },
        provider="yfinance",
    )


def _write_snapshot(path: Path, snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# find_latest_snapshot
# ---------------------------------------------------------------------------


def test_find_latest_snapshot_picks_newest_by_date(tmp_path: Path) -> None:
    subdir = tmp_path / "bb_forex"
    subdir.mkdir()
    for name in ["2026-04-18.json", "2026-04-19.json", "2026-04-20.json"]:
        (subdir / name).write_text("{}", encoding="utf-8")

    result = agg.find_latest_snapshot(subdir)
    assert result is not None
    assert result.name == "2026-04-20.json"


def test_find_latest_snapshot_ignores_tmp(tmp_path: Path) -> None:
    subdir = tmp_path / "bb_forex"
    subdir.mkdir()
    (subdir / "2026-04-20.json.tmp").write_text("{}", encoding="utf-8")

    result = agg.find_latest_snapshot(subdir)
    assert result is None


def test_find_latest_snapshot_returns_none_when_empty(tmp_path: Path) -> None:
    subdir = tmp_path / "empty"
    subdir.mkdir()
    assert agg.find_latest_snapshot(subdir) is None


def test_find_latest_snapshot_returns_none_when_missing(tmp_path: Path) -> None:
    subdir = tmp_path / "nonexistent"
    assert agg.find_latest_snapshot(subdir) is None


# ---------------------------------------------------------------------------
# compute_status
# ---------------------------------------------------------------------------


def test_compute_status_ok_when_fresh() -> None:
    snapshot = _forex_snapshot(scraped_at=_NOW - timedelta(hours=1))
    status = agg.compute_status(snapshot, "https://example.com", _NOW)
    assert status.status == "ok"
    assert status.age_hours is not None
    assert 0.9 < status.age_hours < 1.1


def test_compute_status_stale_when_old() -> None:
    snapshot = _forex_snapshot(scraped_at=_NOW - timedelta(hours=48))
    status = agg.compute_status(snapshot, "https://example.com", _NOW)
    assert status.status == "stale"
    assert status.age_hours is not None
    assert status.age_hours > 24.0


def test_compute_status_missing_when_none() -> None:
    status = agg.compute_status(None, "https://example.com", _NOW)
    assert status.status == "missing"
    assert status.last_success is None
    assert status.age_hours is None
    assert status.error is not None


def test_compute_status_handles_naive_scraped_at() -> None:
    """Snapshot with naive datetime (no tzinfo) should still compute correctly."""
    naive_dt = (_NOW - timedelta(hours=1)).replace(tzinfo=None)  # 1 hour before _NOW, naive
    snapshot = _forex_snapshot(scraped_at=naive_dt)
    status = agg.compute_status(snapshot, None, _NOW)
    assert status.status == "ok"
    assert status.age_hours is not None and status.age_hours < 2.0


# ---------------------------------------------------------------------------
# flatten_data
# ---------------------------------------------------------------------------


def test_flatten_data_includes_all_forex_fields() -> None:
    snapshots = {"bb_forex": _forex_snapshot(), "dse_market": None, "commodity_prices": None}
    data = agg.flatten_data(snapshots)
    assert data["usd_bdt_mid"] == 122.7
    assert data["eur_bdt"] == 144.34
    assert data["gbp_bdt"] == 165.85
    assert data["gross_reserves_usd_bn"] == 34.1166
    assert data["reserves_date"] == "2026-03-01"


def test_flatten_data_includes_dse_when_trading_day() -> None:
    snapshots = {"bb_forex": None, "dse_market": _dse_snapshot(), "commodity_prices": None}
    data = agg.flatten_data(snapshots)
    assert data["trading_day"] is True
    assert data["dsex"] == pytest.approx(5232.49)
    assert data["turnover_crore"] == pytest.approx(824.76)
    assert data["advancing"] == 120
    assert data["declining"] == 207


def test_flatten_data_omits_dse_market_when_non_trading_day() -> None:
    snapshot = _dse_snapshot(trading_day=False)
    snapshots = {"bb_forex": None, "dse_market": snapshot, "commodity_prices": None}
    data = agg.flatten_data(snapshots)
    assert data["trading_day"] is False
    assert "dsex" not in data
    assert "turnover_crore" not in data


def test_flatten_data_handles_missing_scrapers() -> None:
    snapshots = {"bb_forex": None, "dse_market": None, "commodity_prices": None}
    data = agg.flatten_data(snapshots)
    assert data == {}


def test_flatten_data_commodity_keys_include_unit() -> None:
    snapshots = {
        "bb_forex": None,
        "dse_market": None,
        "commodity_prices": _commodity_snapshot(),
    }
    data = agg.flatten_data(snapshots)
    assert "brent_crude_usd_barrel" in data
    assert "gold_usd_oz" in data
    assert "commodity_change_pct" in data
    assert "brent_crude" in data["commodity_change_pct"]


# ---------------------------------------------------------------------------
# write_latest
# ---------------------------------------------------------------------------


def test_write_latest_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(agg, "LATEST_PATH", tmp_path / "latest.json")

    bundle = LatestBundle(
        schema_version="1.0",
        updated_at=_NOW,
        sources_status={
            "bb_forex": SourceStatus(status="ok", last_success=_NOW, age_hours=0.5)
        },
        data={"usd_bdt_mid": 122.7},
    )

    agg.write_latest(bundle)

    latest = tmp_path / "latest.json"
    assert latest.exists()
    # .tmp file must be gone after atomic replace
    assert not (tmp_path / "latest.json.tmp").exists()

    payload = json.loads(latest.read_text())
    assert payload["data"]["usd_bdt_mid"] == 122.7
    assert payload["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# main() end-to-end
# ---------------------------------------------------------------------------


def _build_data_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Create fresh snapshots in tmp_path/data/* and a minimal sources.json."""
    data_dir = tmp_path / "data"
    for key, snapshot_fn in [
        ("bb_forex", _forex_snapshot),
        ("dse_market", _dse_snapshot),
        ("commodity_prices", _commodity_snapshot),
    ]:
        _write_snapshot(data_dir / key / "2026-04-20.json", snapshot_fn())

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    config = {
        "sources": {
            "bb_exchange_rates": {"url": "https://example.com/forex"},
            "dse_market_summary": {"url": "https://example.com/dse"},
        }
    }
    cfg_path = cfg_dir / "sources.json"
    cfg_path.write_text(json.dumps(config), encoding="utf-8")

    return data_dir, cfg_path


def test_main_end_to_end_with_all_scrapers_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir, cfg_path = _build_data_tree(tmp_path)
    latest_path = data_dir / "latest.json"

    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", latest_path)
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")

    exit_code = agg.main()
    assert exit_code == 0
    assert latest_path.exists()

    payload = json.loads(latest_path.read_text())
    assert "updated_at" in payload
    assert payload["schema_version"] == "3.0"
    assert payload["sources_status"]["bb_forex"]["status"] == "ok"
    assert payload["sources_status"]["dse_market"]["status"] == "ok"
    assert payload["sources_status"]["commodity_prices"]["status"] == "ok"
    assert "usd_bdt_mid" in payload["data"]
    assert "dsex" in payload["data"]
    assert "brent_crude_usd_barrel" in payload["data"]


def test_main_fires_warning_on_stale_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir, cfg_path = _build_data_tree(tmp_path)
    latest_path = data_dir / "latest.json"

    # Overwrite forex snapshot with a 48-hour-old scraped_at
    stale_time = _NOW - timedelta(hours=48)
    _write_snapshot(
        data_dir / "bb_forex" / "2026-04-20.json",
        _forex_snapshot(scraped_at=stale_time),
    )

    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", latest_path)
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")

    notify_calls: list[tuple] = []

    def _fake_notify(level, title, message, fields=None):
        notify_calls.append((level, title, message))
        return True

    monkeypatch.setattr(agg, "notify", _fake_notify)

    exit_code = agg.main()
    assert exit_code == 0

    # At least one warning call for the stale source
    warning_calls = [c for c in notify_calls if c[0] == "warning"]
    assert warning_calls, "Expected a warning notify call for stale bb_forex"


def test_main_exit_1_on_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir, cfg_path = _build_data_tree(tmp_path)
    latest_path = data_dir / "latest.json"

    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", latest_path)
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")

    # Inject a bad LatestBundle constructor that always raises ValidationError
    original_bundle_cls = agg.LatestBundle

    def _bad_bundle(**kwargs):
        from pydantic import ValidationError as VE

        # Force a ValidationError by using a private Pydantic trick:
        # pass an invalid schema_version type
        return original_bundle_cls(schema_version=None, **{k: v for k, v in kwargs.items() if k != "schema_version"})

    monkeypatch.setattr(agg, "LatestBundle", _bad_bundle)

    notify_calls: list[tuple] = []

    def _fake_notify(level, title, message, fields=None):
        notify_calls.append((level, title, message))
        return True

    monkeypatch.setattr(agg, "notify", _fake_notify)

    exit_code = agg.main()
    assert exit_code == 1

    error_calls = [c for c in notify_calls if c[0] == "error"]
    assert error_calls, "Expected an error notify call on validation failure"


# ---------------------------------------------------------------------------
# v3 dual-shape tests
# ---------------------------------------------------------------------------


def test_aggregator_emits_v3_domains_and_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v3 aggregator emits domains/freshness/alerts when sources-v3.json + per-indicator snapshots exist."""
    import aggregate_latest

    # Override paths to tmp_path
    monkeypatch.setattr(aggregate_latest, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(aggregate_latest, "LATEST_PATH", tmp_path / "data" / "latest.json")
    monkeypatch.setattr(aggregate_latest, "CONFIG_PATH", tmp_path / "config" / "sources.json")
    monkeypatch.setattr(aggregate_latest, "SOURCES_V3_PATH", tmp_path / "config" / "sources-v3.json")

    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "sources.json").write_text(json.dumps({"sources": {}}))
    (tmp_path / "config" / "sources-v3.json").write_text(
        json.dumps(
            {
                "version": "3.0",
                "indicators": [
                    {
                        "id": "policy_rate_slf_sdf",
                        "name": "Policy Rate",
                        "domain": "money_market",
                        "cadence": "daily",
                        "fetch": {"type": "html", "url": "https://www.bb.org.bd/en/"},
                        "parse": {
                            "deterministic": "html_footer_ticker",
                            "value_type": "percent",
                            "valid_range": [0, 25],
                            "llm_prompt": "html_footer_ticker.txt",
                        },
                        "anomaly_threshold": 1.0,
                    }
                ],
            }
        )
    )
    (tmp_path / "data" / "policy_rate_slf_sdf").mkdir()
    (tmp_path / "data" / "policy_rate_slf_sdf" / "2026-04-30.json").write_text(
        json.dumps(
            {
                "indicator_id": "policy_rate_slf_sdf",
                "name": "Policy Rate",
                "domain": "money_market",
                "cadence": "daily",
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "source_url": "https://www.bb.org.bd/en/",
                "value": 10.0,
                "_provenance": "deterministic",
            }
        )
    )

    rc = aggregate_latest.main()
    assert rc == 0

    bundle = json.loads((tmp_path / "data" / "latest.json").read_text())
    assert bundle["schema_version"] == "3.0"
    assert "domains" in bundle
    assert "money_market" in bundle["domains"]
    assert "policy_rate_slf_sdf" in bundle["domains"]["money_market"]
    assert bundle["data"]["policy_rate_slf_sdf"] == 10.0  # also flat in data
    assert bundle["freshness"]["indicators_total"] == 1
    assert bundle["freshness"]["indicators_fresh"] == 1


def test_the_brief_read_paths_still_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: The Brief reads payload['updated_at'], payload['sources_status'][src_id]['status']/['age_hours'],
    and payload['data'].get(key). v3 must not break those exact paths."""
    import aggregate_latest

    monkeypatch.setattr(aggregate_latest, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(aggregate_latest, "LATEST_PATH", tmp_path / "data" / "latest.json")
    monkeypatch.setattr(aggregate_latest, "CONFIG_PATH", tmp_path / "config" / "sources.json")
    monkeypatch.setattr(aggregate_latest, "SOURCES_V3_PATH", tmp_path / "config" / "sources-v3.json")

    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "sources.json").write_text(json.dumps({"sources": {}}))
    # No v3 file at all — emulates pre-v3 install on the target path
    rc = aggregate_latest.main()
    assert rc == 0

    bundle = json.loads((tmp_path / "data" / "latest.json").read_text())
    # The Brief's exact contract:
    assert "updated_at" in bundle
    assert "sources_status" in bundle
    assert "data" in bundle  # flat dict; The Brief calls .get(key) on this
    assert isinstance(bundle["data"], dict)
    # Since no MVP snapshots exist either, all sources should be missing — but the structure is intact
    for status in bundle["sources_status"].values():
        assert "status" in status  # The Brief reads s.get("status")
