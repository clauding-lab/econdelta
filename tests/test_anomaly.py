"""Tests for utils/anomaly.py."""

import json
import tempfile
from pathlib import Path

import pytest

from utils.anomaly import check_threshold, load_thresholds


class TestCheckThreshold:
    def test_anomaly_detected_exceeds_threshold(self):
        # 5500 vs 5000 = 10% change, threshold 5% -> anomalous
        ok, pct = check_threshold("dsex", 5500.0, 5000.0, {"dsex": 0.05})
        assert ok is False
        assert abs(pct - 0.10) < 1e-9

    def test_within_threshold(self):
        # 5100 vs 5000 = 2% change, threshold 5% -> ok
        ok, pct = check_threshold("dsex", 5100.0, 5000.0, {"dsex": 0.05})
        assert ok is True
        assert abs(pct - 0.02) < 1e-9

    def test_first_run_prev_none(self):
        ok, pct = check_threshold("dsex", 5000.0, None, {"dsex": 0.05})
        assert ok is True
        assert pct == 0.0

    def test_first_run_prev_zero(self):
        ok, pct = check_threshold("usd_bdt_mid", 110.0, 0.0, {"usd_bdt_mid": 0.02})
        assert ok is True
        assert pct == 0.0

    def test_exact_threshold_boundary_is_ok(self):
        # exactly 5% change against a 5% threshold -> ok (<=)
        ok, pct = check_threshold("dsex", 5250.0, 5000.0, {"dsex": 0.05})
        assert ok is True
        assert abs(pct - 0.05) < 1e-9

    def test_unknown_metric_uses_default_10pct_threshold(self):
        # 14% change against default 10% -> anomalous
        ok, pct = check_threshold("unknown_metric", 1.14, 1.0, {})
        assert ok is False

        # 9% change against default 10% -> ok
        ok2, pct2 = check_threshold("unknown_metric", 1.09, 1.0, {})
        assert ok2 is True

    def test_decrease_also_triggers_anomaly(self):
        # 4500 vs 5000 = 10% down, threshold 5% -> anomalous
        ok, pct = check_threshold("dsex", 4500.0, 5000.0, {"dsex": 0.05})
        assert ok is False
        assert abs(pct - 0.10) < 1e-9

    def test_multiple_metrics_in_thresholds(self):
        thresholds = {"dsex": 0.05, "usd_bdt_mid": 0.02, "gold": 0.06}
        ok, _ = check_threshold("usd_bdt_mid", 112.5, 110.0, thresholds)
        # 2.5/110 ~ 2.27% > 2% threshold -> anomalous
        assert ok is False


class TestLoadThresholds:
    def test_loads_valid_json(self):
        data = {
            "_meta": {"description": "test"},
            "dsex": 0.05,
            "gold": 0.06,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        result = load_thresholds(tmp_path)
        assert result["dsex"] == 0.05
        assert result["gold"] == 0.06
        assert "_meta" not in result

    def test_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_thresholds("/nonexistent/path/thresholds.json")

    def test_loads_actual_config_file(self):
        config_path = Path(__file__).parent.parent / "config" / "thresholds.json"
        result = load_thresholds(config_path)
        assert "dsex" in result
        assert "usd_bdt_mid" in result
        assert "_meta" not in result
