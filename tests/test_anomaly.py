"""Tests for utils/anomaly.py."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from utils.anomaly import (
    CORRIDOR_REPO_ID,
    CORRIDOR_SDF_ID,
    CORRIDOR_SLF_ID,
    check_corridor_coherence,
    check_threshold,
    load_thresholds,
)


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


class TestCheckCorridorCoherence:
    """The BB policy corridor invariant SDF <= repo <= SLF (E1.4).

    The three legs are parsed independently, so this cross-metric check runs at
    aggregate time on the assembled `data` dict. It detects-and-alerts: a
    mis-ordered corridor fires one loud notify; it never rejects the run.
    """

    def test_misordered_corridor_fires_one_error_notify(self):
        # SDF above repo violates SDF <= repo <= SLF -> loud alert.
        data = {
            CORRIDOR_SDF_ID: 8.5,
            CORRIDOR_REPO_ID: 8.0,
            CORRIDOR_SLF_ID: 11.5,
        }
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is False
        assert mock_notify.call_count == 1
        args, _kwargs = mock_notify.call_args
        # level is the first positional arg; message body the third.
        assert args[0] == "error"
        message = args[2]
        assert "8.5" in message
        assert "8.0" in message
        assert "11.5" in message
        assert CORRIDOR_SDF_ID in message
        assert CORRIDOR_REPO_ID in message
        assert CORRIDOR_SLF_ID in message

    def test_correct_real_corridor_is_silent(self):
        # Live corridor confirmed 2026-07-10: SDF 7.50 < repo 10.00 < SLF 11.50.
        data = {
            CORRIDOR_SDF_ID: 7.5,
            CORRIDOR_REPO_ID: 10.0,
            CORRIDOR_SLF_ID: 11.5,
        }
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is True
        mock_notify.assert_not_called()

    def test_missing_leg_is_silent(self):
        # A None leg is absent data, not a violation — never false-alarm.
        data = {
            CORRIDOR_SDF_ID: 7.5,
            CORRIDOR_REPO_ID: None,
            CORRIDOR_SLF_ID: 11.5,
        }
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is True
        mock_notify.assert_not_called()

    def test_absent_key_is_silent(self):
        # An entirely missing leg (key not in data) is also silent.
        data = {CORRIDOR_SDF_ID: 7.5, CORRIDOR_SLF_ID: 11.5}
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is True
        mock_notify.assert_not_called()

    def test_equal_legs_are_coherent(self):
        # SDF == repo == SLF satisfies the <= boundary — no alert.
        data = {
            CORRIDOR_SDF_ID: 10.0,
            CORRIDOR_REPO_ID: 10.0,
            CORRIDOR_SLF_ID: 10.0,
        }
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is True
        mock_notify.assert_not_called()


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
