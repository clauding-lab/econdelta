import pytest
from claude_max.validators import (
    InvalidValueError,
    validate_value,
    values_match,
)


def test_validate_value_passes_for_in_range_percent():
    validate_value(value=10.0, value_type="percent", valid_range=(0.5, 25.0))


def test_validate_value_rejects_out_of_range():
    with pytest.raises(InvalidValueError, match="out of range"):
        validate_value(value=99.0, value_type="percent", valid_range=(0.5, 25.0))


def test_validate_value_rejects_wrong_type():
    with pytest.raises(InvalidValueError, match="expected number"):
        validate_value(value="abc", value_type="percent", valid_range=(0.5, 25.0))  # type: ignore[arg-type]


def test_values_match_floats_within_relative_tolerance():
    assert values_match(100.0, 100.4, value_type="percent")
    assert values_match(100.0, 100.6, value_type="percent") is False


def test_values_match_int_strict_equality():
    assert values_match(5, 5, value_type="count")
    assert values_match(5, 6, value_type="count") is False
