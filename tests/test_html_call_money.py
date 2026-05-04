from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.html_call_money  # noqa: F401
from fetchers.base import FetchResult
from parsers.registry import get_parser

_HTML = """
<table>
<tr><th>Tenor</th><th>Rate (%)</th></tr>
<tr><td>1D</td><td>9.50</td></tr>
<tr><td>7D</td><td>9.75</td></tr>
<tr><td>14D</td><td>10.10</td></tr>
<tr><td>90D</td><td>10.50</td></tr>
</table>
"""


@pytest.fixture
def artifact(tmp_path: Path):
    p = tmp_path / "p.html"
    p.write_text(_HTML)
    return FetchResult(
        indicator_id="call_money_rate", artifact_path=p, artifact_type="html",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False,
    )


def test_extracts_all_tenors_as_dict(artifact):
    p = get_parser("html_call_money")
    r = p.parse(artifact, instruction="all_tenors")
    assert r.value == {"1D": 9.50, "7D": 9.75, "14D": 10.10, "90D": 10.50}
