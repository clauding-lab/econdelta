"""E2.4 — off-box export of irreplaceable history."""
from __future__ import annotations

import json

import pytest

from scripts.export_history import (
    ExportError,
    export_history,
    is_rescrapable_daily,
    paginate_table,
)

_MONTHLY = [
    {"metric_id": "cpi_headline_monthly", "as_of": "2026-06-01", "value": 9.1},
    {"metric_id": "fiscal_bank_borrow_monthly", "as_of": "2026-05-01", "value": 2862},
]
_DAILY_TABLE = [
    {"metric_id": "money_multiplier", "as_of": "2026-05-31", "value": 5.37, "source": "EconDelta"},
    {"metric_id": "dsex", "as_of": "2026-07-08", "value": 5804.0, "source": "EconDelta"},
    {"metric_id": "dse_close_GP", "as_of": "2026-07-08", "value": 320.0, "source": "DSE"},
]


def _fetcher(table):
    return {"metric_history_monthly": _MONTHLY, "metric_history": _DAILY_TABLE}[table]


def test_export_writes_both_tables_with_manifest(tmp_path):
    out = export_history(tmp_path, fetcher=_fetcher)
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["manifest"] == {"metric_history_monthly": 2, "metric_history": 3}
    assert payload["tier"] == "all"
    assert len(payload["tables"]["metric_history"]) == 3
    assert out.name.startswith("econdelta_history_export_")


def test_irreplaceable_only_drops_rescrapable_daily(tmp_path):
    out = export_history(tmp_path, fetcher=_fetcher, irreplaceable_only=True)
    payload = json.loads(out.read_text())
    kept = {r["metric_id"] for r in payload["tables"]["metric_history"]}
    assert kept == {"money_multiplier"}       # dsex + dse_close_GP dropped
    assert payload["manifest"]["metric_history_monthly"] == 2  # monthly untouched
    assert payload["tier"] == "irreplaceable_only"


def test_is_rescrapable_daily_predicate():
    daily_cfg = frozenset({"call_money_rate"})
    assert is_rescrapable_daily("dsex", daily_cfg) is True
    assert is_rescrapable_daily("dse_close_XYZ", daily_cfg) is True
    assert is_rescrapable_daily("call_money_rate", daily_cfg) is True
    assert is_rescrapable_daily("money_multiplier", daily_cfg) is False
    assert is_rescrapable_daily("cpi_headline_monthly", daily_cfg) is False


def test_paginate_raises_without_credentials(monkeypatch):
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ExportError):
        paginate_table("metric_history_monthly")
