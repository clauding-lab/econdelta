"""Tests for utils/schema.py."""

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from utils.schema import (
    ForexRates,
    ForexReserves,
    ForexSnapshot,
    LatestBundle,
    SourceStatus,
)

_NOW = datetime.now(timezone.utc)
_TODAY = date.today()


class TestForexSnapshot:
    def _valid_rates(self) -> dict:
        return {
            "usd_bdt_mid": 110.25,
            "usd_bdt_buy": 109.90,
            "usd_bdt_sell": 110.60,
            "eur_bdt": 119.50,
            "gbp_bdt": 138.20,
            "source_url": "https://bb.org.bd/forex/rates",
        }

    def _valid_snapshot(self) -> dict:
        return {
            "date": _TODAY,
            "scraped_at": _NOW,
            "rates": self._valid_rates(),
        }

    def test_accepts_valid_data(self):
        snapshot = ForexSnapshot(**self._valid_snapshot())
        assert snapshot.rates.usd_bdt_mid == 110.25
        assert snapshot.schema_version == "1.0"
        assert snapshot.reserves is None

    def test_accepts_with_reserves(self):
        data = self._valid_snapshot()
        data["reserves"] = {
            "gross_reserves_usd_bn": 21.5,
            "import_cover_months": 4.2,
            "reserves_date": _TODAY,
            "source_url": "https://bb.org.bd/reserves",
        }
        snapshot = ForexSnapshot(**data)
        assert snapshot.reserves is not None
        assert snapshot.reserves.gross_reserves_usd_bn == 21.5

    def test_rejects_extra_fields(self):
        data = self._valid_snapshot()
        data["unexpected_field"] = "should fail"

        with pytest.raises(ValidationError) as exc_info:
            ForexSnapshot(**data)

        assert "unexpected_field" in str(exc_info.value)

    def test_rejects_missing_required_field(self):
        data = self._valid_snapshot()
        rates = data["rates"].copy()
        del rates["usd_bdt_mid"]
        data["rates"] = rates

        with pytest.raises(ValidationError):
            ForexSnapshot(**data)

    def test_frozen_model_rejects_mutation(self):
        snapshot = ForexSnapshot(**self._valid_snapshot())

        with pytest.raises(Exception):  # ValidationError or TypeError depending on pydantic version
            snapshot.schema_version = "2.0"  # type: ignore[misc]


class TestSourceStatus:
    def test_ok_status(self):
        s = SourceStatus(status="ok", age_hours=1.5, url="https://example.com")
        assert s.status == "ok"
        assert s.error is None

    def test_failed_status_with_error(self):
        s = SourceStatus(status="failed", error="ConnectionError: timed out")
        assert s.status == "failed"
        assert s.error is not None

    def test_rejects_invalid_status_literal(self):
        with pytest.raises(ValidationError):
            SourceStatus(status="unknown")

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            SourceStatus(status="ok", bogus_key="val")


class TestLatestBundle:
    def test_validates_nested_source_status(self):
        bundle = LatestBundle(
            updated_at=_NOW,
            sources_status={
                "bb_forex": SourceStatus(status="ok", age_hours=0.5),
                "dse_market": SourceStatus(status="stale", age_hours=26.0),
            },
            data={"usd_bdt_mid": 110.25},
        )
        assert bundle.sources_status["bb_forex"].status == "ok"
        assert bundle.sources_status["dse_market"].status == "stale"
        assert bundle.schema_version == "1.0"

    def test_rejects_invalid_source_status_in_dict(self):
        with pytest.raises((ValidationError, Exception)):
            LatestBundle(
                updated_at=_NOW,
                sources_status={"bb_forex": {"status": "bad_status"}},
                data={},
            )

    def test_data_accepts_arbitrary_keys(self):
        bundle = LatestBundle(
            updated_at=_NOW,
            sources_status={},
            data={"nested": {"deeply": [1, 2, 3]}, "flag": True},
        )
        assert bundle.data["flag"] is True
