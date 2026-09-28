"""Pydantic models for scraper snapshots and latest.json bundle."""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


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
    # Only set on type="stale_fallback": how many days old the republished
    # reading is. Optional so every existing alert stays valid unchanged.
    age_days: int | None = None


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
    """BB foreign exchange reserves snapshot.

    ``bpm6_reserves_usd_bn`` is additive (2026-08, D5 reserves-memo split):
    older snapshot files written before this field existed simply validate
    with it defaulting to ``None`` — nothing about the pre-existing
    ``gross_reserves_usd_bn`` value or any stored row changes. It is
    ``None`` only when BB's reserves table genuinely didn't carry a BPM6
    column for that read; ``scrapers.bb_forex.parse_reserves`` refuses the
    write entirely (raises ``ParseError``) rather than accept a BPM6 value
    that fails the ``bpm6 < gross`` cross-column invariant.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gross_reserves_usd_bn: float
    bpm6_reserves_usd_bn: float | None = None
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
    # This ticker's OWN yfinance quote date (history()'s DatetimeIndex),
    # independent of the other tickers in the same snapshot. L1 (2026-08-22
    # round-1 review): CommoditySnapshot.date is the MAX across all three
    # tickers (brent/WTI/gold trade nearly 24/5 so they virtually always
    # agree) -- this per-id field is what lets a consumer stamp each
    # commodity's own as_of from ITS quote, not a shared snapshot-wide max,
    # for the rare run where one ticker's quote genuinely lags the others.
    # None when this ticker's own history() call failed this run (never
    # fabricated -- see scrapers/commodity_prices.py::_quote_date_from_history).
    quote_date: date | None = None


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


ReceiptStatus = Literal["ok", "failed", "skipped"]
# Strict: a bool, "2" or 2.0 is a producer bug, not a row count (the Brief reader rejects them too).
RowCount = Annotated[int, Field(strict=True, ge=0)]
LagDays = Annotated[int, Field(strict=True, gt=0)]


def _without_absent(handler: SerializerFunctionWrapHandler, model: BaseModel) -> dict[str, Any]:
    """An unset optional receipt field is left out, never written as null."""
    return {k: v for k, v in handler(model).items() if v is not None}


class ReceiptFailure(BaseModel):
    """One failed operation of a persistence attempt (utils/write_receipts.py)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    category: str
    detail: str


class ReceiptSkip(BaseModel):
    """One reason nothing was written; a skip is never a persistence failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    detail: str
    # Publication lag only: the newest official month already stored (R2 fix 4).
    newest_recorded: date | None = None
    # Publication lag only: the accepted lag window for that series, in days (R2 fix 4 round 1:
    # the sentinel's vintage grace for its cadence, ruling E1).
    lag_window_days: LagDays | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _without_absent(handler, self)


class WriteReceipt(BaseModel):
    """The outcome of one persistence stage or leg (R2 fix 10).

    `ok` means rows were written AND confirmed; `failed` means a write, read or
    readback failed or went unconfirmed; `skipped` means nothing needed writing.
    A leg's failure dominates its parent, and confirmed rows are never erased.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ReceiptStatus
    attempted_at: AwareDatetime
    reason: str | None = None
    confirmed_rows: RowCount | None = None
    failures: tuple[ReceiptFailure, ...] | None = None
    skips: tuple[ReceiptSkip, ...] | None = None
    legs: "dict[str, WriteReceipt] | None" = None

    @model_validator(mode="after")
    def _status_agrees_with_its_evidence(self) -> "WriteReceipt":
        if self.failures and self.status != "failed":
            raise ValueError(f"a receipt with failures cannot be {self.status!r}")
        if self.failures is not None and self.confirmed_rows is not None:
            implied = "failed" if self.failures else "ok" if self.confirmed_rows else "skipped"
            if self.status != implied:
                raise ValueError(f"status {self.status!r} contradicts its rows/failures ({implied!r})")
        if self.legs:
            self._agrees_with_legs(self.legs)
        return self

    def _agrees_with_legs(self, legs: "dict[str, WriteReceipt]") -> None:
        states = {leg.status for leg in legs.values()}
        implied = "failed" if "failed" in states else "ok" if "ok" in states else "skipped"
        if self.status != implied:
            raise ValueError(f"status {self.status!r} hides its legs' outcome ({implied!r})")
        rows = sum(leg.confirmed_rows or 0 for leg in legs.values())
        if self.confirmed_rows is not None and self.confirmed_rows != rows:
            raise ValueError(f"{self.confirmed_rows} confirmed rows but its legs confirm {rows}")

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _without_absent(handler, self)


class WriteStatus(BaseModel):
    """Database persistence outcome recorded in the snapshot, after the attempts.

    `daily` is the main metric_history write alone; `media_overrides` is the
    approved-press re-assertion that runs after it, reported on its own so an
    override problem is neither charged to nor hidden by the daily write.
    Absent from pre-receipt snapshots (LatestBundle.write_status is None).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    daily: WriteReceipt
    monthly: WriteReceipt
    media_overrides: WriteReceipt | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _without_absent(handler, self)


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
    write_status: WriteStatus | None = None  # None: a pre-receipt producer, never a success
    observations: dict[str, dict[str, Any]] = {}
    domains: dict[str, dict[str, Any]] = {}
    freshness: FreshnessSummary | None = None
    alerts: list[Alert] = []
