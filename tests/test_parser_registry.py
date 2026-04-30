import pytest

from parsers.base import ParseResult
from parsers.registry import REGISTRY, get_parser, register


def test_register_adds_to_registry():
    @register("dummy_test_parser")
    class _D:
        def parse(self, artifact, instruction):
            return ParseResult(value=1.0)

    assert "dummy_test_parser" in REGISTRY
    p = get_parser("dummy_test_parser")
    assert p is not None


def test_get_parser_unknown_raises():
    with pytest.raises(KeyError):
        get_parser("nonexistent_parser")
