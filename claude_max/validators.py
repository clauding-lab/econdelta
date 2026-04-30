"""Value sanity validators used by the hybrid parser.

A value is "valid" if it matches its declared `value_type` and falls inside
its `valid_range`. Two values "match within tolerance" if they're equal
(strict for ints / counts) or within 0.5% relative diff (floats).
"""
from __future__ import annotations

from typing import Final, Literal

ValueType = Literal["percent", "amount_bdt_crore", "amount_usd_bn", "ratio", "count", "rate"]

_FLOAT_RELATIVE_TOLERANCE: Final[float] = 0.005


class InvalidValueError(ValueError):
    """Raised when a value fails type or range validation."""


def validate_value(*, value: object, value_type: ValueType, valid_range: tuple[float, float]) -> None:
    if value_type == "count":
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidValueError(f"expected int for {value_type}, got {type(value).__name__}")
    else:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise InvalidValueError(f"expected number for {value_type}, got {type(value).__name__}")

    lo, hi = valid_range
    if not (lo <= float(value) <= hi):
        raise InvalidValueError(f"value {value} out of range [{lo}, {hi}] for {value_type}")


def values_match(a: float | int, b: float | int, *, value_type: ValueType) -> bool:
    if value_type == "count":
        return a == b
    if a == 0 and b == 0:
        return True
    denominator = max(abs(a), abs(b))
    return abs(a - b) / denominator <= _FLOAT_RELATIVE_TOLERANCE
