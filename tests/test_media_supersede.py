from datetime import date

from media_screen.supersede import is_superseded

PRESS = date(2026, 3, 31)


def test_fresher_not_superseded_while_bb_lags():
    assert is_superseded(kind="fresher_period", press_as_of=PRESS, parsed_baseline=35.73,
                         automated_value=35.73, automated_as_of=date(2025, 9, 30)) is False


def test_fresher_superseded_when_bb_reaches_period():
    assert is_superseded(kind="fresher_period", press_as_of=PRESS, parsed_baseline=35.73,
                         automated_value=31.0, automated_as_of=date(2026, 3, 31)) is True


def test_same_period_held_while_bb_value_unchanged():
    assert is_superseded(kind="same_period_conflict", press_as_of=PRESS, parsed_baseline=35.73,
                         automated_value=35.73, automated_as_of=PRESS) is False


def test_same_period_superseded_when_bb_revises():
    assert is_superseded(kind="same_period_conflict", press_as_of=PRESS, parsed_baseline=35.73,
                         automated_value=34.10, automated_as_of=PRESS) is True


def test_no_automated_data_means_not_superseded():
    assert is_superseded(kind="fresher_period", press_as_of=PRESS, parsed_baseline=None,
                         automated_value=None, automated_as_of=None) is False
