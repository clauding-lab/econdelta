"""Parser strategy registry.

Decorator pattern: @register("name") attaches an instance to REGISTRY. The
hybrid orchestrator looks up parsers by the `parse.deterministic` field in
sources-v3.json.
"""
from __future__ import annotations

from typing import Protocol

from fetchers.base import FetchResult
from parsers.base import ParseResult


class Parser(Protocol):
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult: ...


REGISTRY: dict[str, Parser] = {}


def register(name: str):
    def decorator(cls: type) -> type:
        REGISTRY[name] = cls()
        return cls
    return decorator


def get_parser(name: str) -> Parser:
    if name not in REGISTRY:
        raise KeyError(f"no parser registered for {name!r}; have: {sorted(REGISTRY)}")
    return REGISTRY[name]
