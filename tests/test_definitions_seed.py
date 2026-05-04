"""Tests for upsert_metric_definitions_seed in utils/supabase_writer.py."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


class TestUpsertMetricDefinitionsSeed:
    def test_returns_count_when_skipped(self):
        from utils.supabase_writer import upsert_metric_definitions_seed
        # When SKIP_SUPABASE=1, the function returns 0 (no rows inserted)
        defs = [
            {"metric_id": "test1", "label": "Test 1", "domain": "Test"},
            {"metric_id": "test2", "label": "Test 2", "domain": "Test"},
        ]
        rc = upsert_metric_definitions_seed(defs)
        assert rc == 0

    def test_handles_empty_list(self):
        from utils.supabase_writer import upsert_metric_definitions_seed
        rc = upsert_metric_definitions_seed([])
        assert rc == 0

    def test_validates_required_fields(self):
        from utils.supabase_writer import upsert_metric_definitions_seed
        # Missing metric_id should raise
        with pytest.raises((KeyError, ValueError)):
            upsert_metric_definitions_seed([{"label": "x", "domain": "y"}])

    def test_default_fields_filled_in(self):
        from utils.supabase_writer import _normalize_definition
        d = _normalize_definition({"metric_id": "test", "label": "Test", "domain": "Test"})
        assert d["sort_order"] == 100
        assert d["format"] == "comma-2dp"
        assert d["is_hero"] is False
        assert d["inverted"] is False
