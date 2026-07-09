"""Tests for utils.supabase_writer.upsert_metric_history.

Mocks the requests.Session so no real Supabase call goes out. Verifies:
  - filtering of non-numeric values
  - bool excluded (it's int subclass in Python)
  - PostgREST endpoint shape with on_conflict
  - auth headers
  - batching behaviour
  - error mapping (network, non-2xx)
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
import requests

from utils.supabase_writer import (
    SupabaseWriteError,
    _rows_from_data,
    upsert_metric_history,
)


def _make_session(status: int = 201, text: str = "") -> MagicMock:
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    sess.post.return_value = resp
    return sess


def test_rows_from_data_filters_non_numeric():
    data = {
        "policy_rate": 10.0,
        "macro_cpi_food": 8.29,
        "live_status": "ok",            # str — skip
        "config_block": {"foo": "bar"},  # dict — skip
        "is_trading_day": True,           # bool — skip even though int subclass
        "trading_volume": 0,              # int 0 — keep (real reading)
        "negative_npl_change": -2.4,      # negative float — keep
        "broken": None,                   # None — skip
    }
    rows = _rows_from_data(data, date(2026, 5, 2), "EconDelta")
    metric_ids = {r["metric_id"] for r in rows}
    assert metric_ids == {"policy_rate", "macro_cpi_food", "trading_volume", "negative_npl_change"}


def test_upsert_posts_to_postgrest_with_on_conflict_clause():
    sess = _make_session()
    n = upsert_metric_history(
        data={"npl": 35.73},
        as_of=date(2026, 5, 2),
        url="https://example.supabase.co",
        service_key="sk_test_123",
        session=sess,
    )
    assert n == 1
    args, kwargs = sess.post.call_args
    url = args[0]
    assert url == "https://example.supabase.co/rest/v1/metric_history?on_conflict=metric_id,as_of"
    assert kwargs["headers"]["apikey"] == "sk_test_123"
    assert kwargs["headers"]["Authorization"] == "Bearer sk_test_123"
    assert "merge-duplicates" in kwargs["headers"]["Prefer"]
    payload = kwargs["json"]
    assert len(payload) == 1
    row = payload[0]
    assert row["metric_id"] == "npl"
    assert row["as_of"] == "2026-05-02"
    assert row["value"] == 35.73
    assert row["source"] == "EconDelta"
    # E1.1: ingested_at is posted so a merge-upsert (ON CONFLICT DO UPDATE) bumps
    # the write-liveness timestamp instead of freezing it at first-insert time.
    assert "ingested_at" in row and row["ingested_at"]


def test_upsert_returns_zero_when_no_numeric_values():
    sess = _make_session()
    n = upsert_metric_history(
        data={"all_strings": "nope", "config": {"k": "v"}},
        as_of=date(2026, 5, 2),
        url="https://example.supabase.co",
        service_key="sk_test_123",
        session=sess,
    )
    assert n == 0
    sess.post.assert_not_called()


def test_upsert_raises_on_non_2xx_response():
    sess = _make_session(status=401, text="invalid_jwt")
    with pytest.raises(SupabaseWriteError, match="HTTP 401"):
        upsert_metric_history(
            data={"x": 1.0},
            as_of=date(2026, 5, 2),
            url="https://example.supabase.co",
            service_key="bad_key",
            session=sess,
        )


def test_upsert_raises_on_network_error():
    sess = MagicMock(spec=requests.Session)
    sess.post.side_effect = requests.exceptions.ConnectionError("dns lookup failed")
    with pytest.raises(SupabaseWriteError, match="network error"):
        upsert_metric_history(
            data={"x": 1.0},
            as_of=date(2026, 5, 2),
            url="https://example.supabase.co",
            service_key="sk",
            session=sess,
        )


def test_upsert_raises_when_credentials_missing(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    with pytest.raises(SupabaseWriteError, match="SUPABASE_URL"):
        upsert_metric_history(data={"x": 1.0}, as_of=date(2026, 5, 2))


def test_upsert_picks_credentials_from_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://from-env.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sk_env_456")
    sess = _make_session()
    upsert_metric_history(
        data={"x": 1.0}, as_of=date(2026, 5, 2), session=sess,
    )
    url = sess.post.call_args[0][0]
    # Trailing slash is stripped, scheme preserved.
    assert url.startswith("https://from-env.supabase.co/rest/v1/metric_history")
    assert sess.post.call_args[1]["headers"]["apikey"] == "sk_env_456"


def test_upsert_batches_large_payloads():
    """500 row batch limit — sending 1200 rows should trigger 3 POSTs."""
    data = {f"metric_{i:04d}": float(i) for i in range(1200)}
    sess = _make_session()
    n = upsert_metric_history(
        data=data, as_of=date(2026, 5, 2),
        url="https://example.supabase.co", service_key="sk", session=sess,
    )
    assert n == 1200
    assert sess.post.call_count == 3


# ---------------------------------------------------------------------------
# Observability — silent drops now warn (regression guard for PR #31 class).
# ---------------------------------------------------------------------------

def test_rows_from_data_scalar_does_not_warn(caplog):
    """Scalar values build a row and produce no warning — keep the happy path quiet."""
    with caplog.at_level("WARNING", logger="supabase_writer"):
        rows = _rows_from_data({"foo": 1.0}, date(2026, 5, 2), "EconDelta")
    assert len(rows) == 1
    assert rows[0]["metric_id"] == "foo"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_rows_from_data_dict_triggers_warning(caplog):
    """A dict value is dropped (no row built) and logs a warning naming the metric_id and type."""
    with caplog.at_level("WARNING", logger="supabase_writer"):
        rows = _rows_from_data({"foo": {"a": 1.0}}, date(2026, 5, 2), "EconDelta")
    assert rows == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "metric_id=foo" in msg
    assert "type=dict" in msg


def test_rows_from_data_bool_stays_silent(caplog):
    """Booleans are filtered without a warning — pre-existing behavior, by design."""
    with caplog.at_level("WARNING", logger="supabase_writer"):
        rows = _rows_from_data({"foo": True}, date(2026, 5, 2), "EconDelta")
    assert rows == []
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_rows_from_data_unknown_str_triggers_warning(caplog):
    """An unexpected string value warns — strings aren't numeric history."""
    with caplog.at_level("WARNING", logger="supabase_writer"):
        rows = _rows_from_data({"foo": "bar"}, date(2026, 5, 2), "EconDelta")
    assert rows == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "metric_id=foo" in msg
    assert "type=str" in msg


def test_rows_from_data_none_triggers_warning(caplog):
    """A None value warns — usually a parser-returned-nothing bug worth surfacing."""
    with caplog.at_level("WARNING", logger="supabase_writer"):
        rows = _rows_from_data({"foo": None}, date(2026, 5, 2), "EconDelta")
    assert rows == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "type=NoneType" in warnings[0].getMessage()


def test_rows_from_data_known_non_history_keys_silent(caplog):
    """Known metadata keys (reserves_date, trading_day, nbr_fytd_cross_check,
    commodity_change_pct) are skipped silently — they're by-design non-numeric
    and would otherwise spam a warning on every successful aggregate run.
    """
    data = {
        "reserves_date": "2026-05-28",
        "trading_day": "2026-05-28",
        "nbr_fytd_cross_check": "single_source_tax_revenue",
        "commodity_change_pct": {"brent": 1.2, "gold": -0.3},
        "usd_bdt_mid": 122.75,  # control: this one SHOULD become a row
    }
    with caplog.at_level("WARNING", logger="supabase_writer"):
        rows = _rows_from_data(data, date(2026, 5, 2), "EconDelta")
    assert len(rows) == 1
    assert rows[0]["metric_id"] == "usd_bdt_mid"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_rows_from_data_known_key_with_unexpected_shape_stays_silent(caplog):
    """If a known-metadata key arrives with an unexpected shape (e.g. None
    from a parser failure), still skip silently — the allow-list is the
    authority on whether to warn. The shape isn't the signal here; the
    indicator-id classification is.
    """
    with caplog.at_level("WARNING", logger="supabase_writer"):
        rows = _rows_from_data(
            {"reserves_date": None}, date(2026, 5, 2), "EconDelta",
        )
    assert rows == []
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# ---------------------------------------------------------------------------
# E1.1 — ingested_at bump makes write-liveness observable even when as_of stalls
#
# The 22-indicator freeze: a slow-cadence metric whose as_of is correctly pinned
# to a recovered reporting vintage is re-written to the SAME (metric_id, as_of)
# row every run. Its value updates in place, but without posting ingested_at the
# write timestamp froze at first-insert — so a live pipeline read as "stale for
# weeks". Every posted row must now carry a fresh ingested_at.
# ---------------------------------------------------------------------------


def test_every_posted_row_carries_ingested_at():
    from datetime import datetime, timezone

    stamp = datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc)
    rows = _rows_from_data(
        {"money_multiplier": 5.37, "debt_domestic_stock_cr": 1247151},
        date(2026, 7, 9), "EconDelta", ingested_at=stamp,
    )
    assert rows, "expected numeric rows"
    assert all(r["ingested_at"] == stamp.isoformat() for r in rows)


def test_recovered_vintage_row_still_gets_fresh_ingested_at():
    """The regression guard: an indicator with a recovered source_as_of (vintage
    as_of pinned in the past) must STILL emit a fresh-write signal each run — its
    as_of is the old reporting period, but ingested_at is today's run time. If
    these two ever coincide again, the freshness signal is dead."""
    from datetime import datetime, timezone

    run_time = datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc)
    rows = _rows_from_data(
        {"debt_domestic_stock_cr": 1247151},
        as_of=date(2026, 7, 9),
        source="EconDelta",
        source_as_of_map={"debt_domestic_stock_cr": date(2025, 12, 31)},
        ingested_at=run_time,
    )
    assert len(rows) == 1
    row = rows[0]
    # as_of is the recovered reporting vintage (correctly stalled)...
    assert row["as_of"] == "2025-12-31"
    # ...but ingested_at is the fresh run time — the observable write-liveness.
    assert row["ingested_at"] == run_time.isoformat()
    assert row["ingested_at"][:10] != row["as_of"], "fresh write must be distinguishable from a stalled as_of"


def test_ingested_at_defaults_to_now_when_unset():
    from datetime import datetime, timezone

    before = datetime.now(timezone.utc)
    rows = _rows_from_data({"policy_rate_repo": 10.0}, date(2026, 7, 9), "EconDelta")
    after = datetime.now(timezone.utc)
    assert len(rows) == 1
    stamped = datetime.fromisoformat(rows[0]["ingested_at"])
    assert before <= stamped <= after
