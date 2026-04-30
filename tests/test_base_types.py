from datetime import datetime, timezone
from pathlib import Path

from fetchers.base import FetchError, FetchResult
from parsers.base import ParseError, ParseResult


def test_fetch_result_is_frozen_dataclass(tmp_path: Path):
    artifact = tmp_path / "x.pdf"
    artifact.write_bytes(b"hi")
    fr = FetchResult(
        indicator_id="x",
        artifact_path=artifact,
        artifact_type="pdf",
        fetched_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        source_url="https://example.com",
        sha256="ab" * 32,
        cache_hit=False,
    )
    assert fr.indicator_id == "x"


def test_parse_result_carries_provenance():
    pr = ParseResult(value=10.0, _provenance="deterministic", _parse_strategy="html_footer_ticker")
    assert pr.value == 10.0
    assert pr._provenance == "deterministic"


def test_fetch_error_is_runtime_error():
    assert issubclass(FetchError, RuntimeError)
    assert issubclass(ParseError, RuntimeError)
