"""Pydantic models for scraper snapshots and latest.json bundle."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class FreshnessByCadence(BaseModel):
    """Fresh/expected counts for a single cadence bucket."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fresh: int
    expected: int
    stale_ids: list[str] = []


class FreshnessSummary(BaseModel):
    """Aggregated freshness counters across all v3 indicators."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    indicators_total: int = 0
    indicators_fresh: int = 0
    indicators_stale: int = 0
    indicators_failed: int = 0
    by_cadence: dict[str, FreshnessByCadence] = {}


class Alert(BaseModel):
    """Anomaly or staleness alert for a single indicator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    indicator_id: str
    type: str
    severity: str
    value: float | int | str | None = None
    previous: float | int | str | None = None
    change_pct: float | None = None


class SourceStatus(BaseModel):
    """Freshness and error state for a single data source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "stale", "failed", "missing"]
    last_success: datetime | None = None
    age_hours: float | None = None
    url: str | None = None
    error: str | None = None


class ForexRates(BaseModel):
    """Bangladesh Bank indicative foreign exchange rates (BDT)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    usd_bdt_mid: float
    usd_bdt_buy: float
    usd_bdt_sell: float
    eur_bdt: float
    gbp_bdt: float
    source_url: str


class ForexReserves(BaseModel):
    """BB foreign exchange reserves snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gross_reserves_usd_bn: float
    import_cover_months: float | None = None
    reserves_date: date
    source_url: str


class ForexSnapshot(BaseModel):
    """Complete forex scrape payload: rates + optional reserves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    date: date
    scraped_at: datetime
    rates: ForexRates
    reserves: ForexReserves | None = None


class DseIndices(BaseModel):
    """DSE index levels and change for a single trading day."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dsex: float
    dsex_change: float
    dsex_change_pct: float
    ds30: float | None = None
    dses: float | None = None


class DseMarket(BaseModel):
    """DSE market-wide breadth and turnover for a single trading day."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turnover_crore: float
    total_trades: int
    advancing: int
    declining: int
    unchanged: int


class DseSnapshot(BaseModel):
    """Complete DSE scrape payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    date: date
    scraped_at: datetime
    trading_day: bool
    indices: DseIndices | None = None   # None if non-trading day
    market: DseMarket | None = None
    source_url: str


class CommodityPrice(BaseModel):
    """Price record for a single commodity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    price: float
    prev_close: float | None = None
    change_pct: float | None = None
    currency: str
    unit: str  # e.g. "barrel", "oz", "ton"


class CommoditySnapshot(BaseModel):
    """Full commodity prices scrape payload.

    Keys for 'prices': brent_crude, wti_crude, gold
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    date: date
    scraped_at: datetime
    prices: dict[str, CommodityPrice]
    provider: str


class LatestBundle(BaseModel):
    """Top-level latest.json structure consumed by The Brief agent.

    Legacy shape (v1): updated_at, sources_status, data (flat dict)
    v3 additions: schema_version bumped to "3.0", plus domains, freshness, alerts.

    'data' is intentionally typed as dict[str, Any] because the flat merge
    shape varies by run — schema enforcement happens at the scraper layer.
    v3 indicators also land in 'data' as flat keys so The Brief can use them
    via snapshot.get("<id>") with no Brief code changes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "3.0"
    updated_at: datetime
    sources_status: dict[str, SourceStatus]
    data: dict[str, Any]
    domains: dict[str, dict[str, Any]] = {}
    freshness: FreshnessSummary | None = None
    alerts: list[Alert] = []
