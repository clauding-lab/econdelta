from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import parsers.html_call_money  # noqa: F401
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

_FIXTURE = Path(__file__).parent / "fixtures" / "bb_call_money.html"

_HEAD = """
<div class="page_header">Statement of Interbank Money Market Transactions</div>
<table><thead><tr><th>Product</th><th>Maturity</th><th>Amount</th>
<th>Highest</th><th>Lowest</th><th>Average</th><th>Number of Deals</th></tr></thead><tbody>
"""
_TAIL = "</tbody></table>"


def _row(product: str, days: int, avg: str) -> str:
    return (f"<tr><td>{product}</td><td>{days} Day(/s)</td><td>100.00</td>"
            f"<td>11.00</td><td>8.00</td><td>{avg}</td><td>5</td></tr>")


def _hdr(text: str) -> str:
    return f'<tr><td colspan="7" class="page_header">{text}</td></tr>'


def _artifact(tmp_path: Path, html: str) -> FetchResult:
    p = tmp_path / "p.html"
    p.write_text(html, encoding="utf-8")
    return FetchResult(
        indicator_id="call_money_rate", artifact_path=p, artifact_type="html",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x" * 64, cache_hit=False,
    )


def _parse(art: FetchResult):
    return get_parser("html_call_money").parse(art, instruction="all_tenors")


def test_real_page_extracts_average_rates_and_page_date():
    art = FetchResult(
        indicator_id="call_money_rate", artifact_path=_FIXTURE, artifact_type="html",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x" * 64, cache_hit=False,
    )
    r = _parse(art)
    # 23 Nov 2026 live page: overnight avg 9.16, 7-day 9.31, 14-day 9.08; no 90-day row.
    assert r.value == {"1D": 9.16, "7D": 9.31, "14D": 9.08}
    assert r.source_as_of == date(2026, 11, 23)
    assert get_parser("html_call_money").recover_source_as_of(art) == date(2026, 11, 23)


def test_reads_only_the_newest_date_block(tmp_path):
    html = (_HEAD + _hdr("22 November, 2026") + _row("Overnight", 1, "9.21") + _row("Short Notice", 7, "9.40")
            + _hdr("23 November, 2026") + _row("Overnight", 1, "9.16") + _TAIL)
    r = _parse(_artifact(tmp_path, html))
    assert r.source_as_of == date(2026, 11, 23)
    # The 22 Nov 7-day must NOT leak into the 23 Nov reading.
    assert r.value == {"1D": 9.16}


def test_ignores_other_maturities(tmp_path):
    html = _HEAD + _hdr("23 November, 2026") + _row("Overnight", 1, "9.16") + _row("Short Notice", 3, "9.20") + _TAIL
    assert _parse(_artifact(tmp_path, html)).value == {"1D": 9.16}


def test_missing_overnight_refuses(tmp_path):
    html = _HEAD + _hdr("23 November, 2026") + _row("Short Notice", 7, "9.31") + _TAIL
    with pytest.raises(ParseError):
        _parse(_artifact(tmp_path, html))


def test_no_dated_header_refuses_and_recovers_none(tmp_path):
    art = _artifact(tmp_path, _HEAD + _row("Overnight", 1, "9.16") + _TAIL)
    with pytest.raises(ParseError):
        _parse(art)
    assert get_parser("html_call_money").recover_source_as_of(art) is None


def test_shifted_column_out_of_range_refuses(tmp_path):
    # An inserted column putting the crore Amount in the Average slot must not
    # publish 4886.99 as a percentage.
    html = _HEAD + _hdr("23 November, 2026") + _row("Overnight", 1, "4886.99") + _TAIL
    with pytest.raises(ParseError):
        _parse(_artifact(tmp_path, html))


def test_legacy_fictional_shape_no_longer_matches(tmp_path):
    # The old parser's expected "1D | 9.50" rows never existed on the live page.
    html = "<table><tr><td>1D</td><td>9.50</td></tr></table>"
    with pytest.raises(ParseError):
        _parse(_artifact(tmp_path, html))
