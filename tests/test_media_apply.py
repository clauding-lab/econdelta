from datetime import date
from unittest.mock import MagicMock

import aggregate_latest as agg


def _override(kind, press_as_of, status="approved", metric="gross_npl_ratio",
              press_value=32.26, parsed_value=35.73):
    return {"id": 9, "metric_id": metric, "parsed_value": parsed_value, "parsed_as_of": "2025-09-30",
            "press_value": press_value, "press_as_of": press_as_of.isoformat(), "kind": kind,
            "source_outlet": "tbsnews", "status": status}


def test_fresher_override_is_written_at_press_period():
    writer, set_status = MagicMock(), MagicMock()
    reader = MagicMock(return_value=[_override("fresher_period", date(2026, 3, 31))])
    # automated pipeline still on the old quarter → NOT superseded
    agg._apply_media_overrides({"gross_npl_ratio": 35.73}, {"gross_npl_ratio": date(2025, 9, 30)},
                               writer=writer, reader=reader, set_status=set_status)
    kwargs = writer.call_args[1]
    assert kwargs["as_of"] == date(2026, 3, 31)
    assert kwargs["source"].startswith("media-approved")
    assert kwargs["data"]["gross_npl_ratio"] == 32.26
    assert "banking_npl_pct" in kwargs["data"]   # alias propagation reached the brief key
    set_status.assert_called_once()
    assert set_status.call_args[0][1] == "applied"


def test_fresher_override_superseded_when_bb_catches_up():
    writer, set_status = MagicMock(), MagicMock()
    reader = MagicMock(return_value=[_override("fresher_period", date(2026, 3, 31), status="applied")])
    # automated pipeline now ON the press period → superseded
    agg._apply_media_overrides({"gross_npl_ratio": 31.0}, {"gross_npl_ratio": date(2026, 3, 31)},
                               writer=writer, reader=reader, set_status=set_status)
    writer.assert_not_called()
    assert set_status.call_args[0][1] == "superseded"


def test_same_period_held_then_superseded_on_revision():
    writer, set_status = MagicMock(), MagicMock()
    held = MagicMock(return_value=[_override("same_period_conflict", date(2025, 9, 30))])
    agg._apply_media_overrides({"gross_npl_ratio": 35.73}, {"gross_npl_ratio": date(2025, 9, 30)},
                               writer=writer, reader=held, set_status=set_status)
    writer.assert_called_once()  # held: press value written
    writer.reset_mock()
    set_status.reset_mock()
    revised = MagicMock(return_value=[_override("same_period_conflict", date(2025, 9, 30), status="applied")])
    agg._apply_media_overrides({"gross_npl_ratio": 34.10}, {"gross_npl_ratio": date(2025, 9, 30)},
                               writer=writer, reader=revised, set_status=set_status)
    writer.assert_not_called()
    assert set_status.call_args[0][1] == "superseded"
