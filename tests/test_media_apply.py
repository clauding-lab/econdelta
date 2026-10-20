from datetime import date
from unittest.mock import MagicMock, call

import pytest

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


@pytest.mark.parametrize("kind, press_as_of", [
    ("fresher_period", date(2026, 3, 31)),        # BB still on the older quarter
    ("same_period_conflict", date(2025, 9, 30)),  # BB unrevised on the same quarter: held
], ids=["fresher period", "same-period conflict held"])
def test_an_already_applied_override_is_re_sent_each_night_but_never_re_marked_applied(kind, press_as_of):
    """Spec D6: an approved press value is re-asserted after every normal upsert until BB
    supersedes it. Only its first send flips 'approved' -> 'applied' and stamps applied_at;
    PATCHing an 'applied' row again each night would reset applied_at to the latest run."""
    writer, set_status = MagicMock(), MagicMock()
    reader = MagicMock(return_value=[_override(kind, press_as_of, status="applied")])
    outcome = agg._apply_media_overrides({"gross_npl_ratio": 35.73}, {"gross_npl_ratio": date(2025, 9, 30)},
                                         writer=writer, reader=reader, set_status=set_status)
    writer.assert_called_once()
    assert (writer.call_args[1]["data"]["gross_npl_ratio"], writer.call_args[1]["as_of"]) == (32.26, press_as_of)
    set_status.assert_not_called()
    assert outcome == {"written": ["gross_npl_ratio"], "failures": []}


def test_only_the_newly_approved_override_is_marked_applied_with_its_applied_time():
    """One run, two active overrides: the newly approved row gets one PATCH to 'applied' with
    applied_at stamped (applied=True); the row already 'applied' is re-sent, not PATCHed."""
    writer, set_status = MagicMock(), MagicMock()
    already = {**_override("fresher_period", date(2026, 3, 31), status="applied", metric="other_metric",
                           press_value=2.0, parsed_value=1.0), "id": 10}
    reader = MagicMock(return_value=[already, _override("fresher_period", date(2026, 3, 31))])
    outcome = agg._apply_media_overrides(
        {"gross_npl_ratio": 35.73, "other_metric": 1.0},
        {"gross_npl_ratio": date(2025, 9, 30), "other_metric": date(2025, 9, 30)},
        writer=writer, reader=reader, set_status=set_status)
    sent = [(c.kwargs["data"].get("other_metric"), c.kwargs["data"].get("gross_npl_ratio"))
            for c in writer.call_args_list]
    assert sent == [(2.0, None), (None, 32.26)]  # both press values went out, one write each
    assert set_status.call_args_list == [call(9, "applied", applied=True)]
    assert outcome == {"written": ["other_metric", "gross_npl_ratio"], "failures": []}
