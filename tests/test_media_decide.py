import pytest

from media_screen.decide import apply_decision


def test_apply_decision_success():
    res = apply_decision(7, "approve", actor="discord:adnan", decider=lambda *a, **k: 1)
    assert res["ok"] is True and "approve" in res["message"] and "7" in res["message"]


def test_apply_decision_noop_when_not_pending():
    res = apply_decision(7, "approve", actor="cli", decider=lambda *a, **k: 0)
    assert res["ok"] is False and "not pending" in res["message"].lower()


def test_apply_decision_propagates_bad_decision():
    def bad(*a, **k):
        raise ValueError("decision must be 'approve' or 'reject'")

    with pytest.raises(ValueError):
        apply_decision(7, "maybe", actor="cli", decider=bad)
