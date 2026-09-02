"""The two gsom.bb.org.bd treasury-outstanding sources.

2026-09-02: `gsom.bb.org.bd/mtm-bill.php` and `/mtm.php` both started
returning a bare "File not found." -- BB rebuilt the Government Securities
Online Market portal onto CodeIgniter-style routes (`index.php/tbill`,
`index.php/tbond`). The extractor recorded the 404 body faithfully
("The page content is 'File not found.'"), wrote value=0.0 with
`_parse_strategy=extract_failed`, and `treasury_bill_outstanding` /
`treasury_bond_outstanding` -- plus the two derived `*_outstanding_cr`
conversions -- went missing from the bundle. Opus then hard-rejected the
whole aggregate run over the missing fields, three nights running.

The rebuilt pages carry the SAME "Total Outstanding Balance:" total row, so
`html_table_row` needs no change at all -- only the URL moved. What did
change is the markup around that row, and that is what the fixture below
pins: the total now lives in a `<tfoot>`, its label cell carries a
`colspan` spanning every data column, and the figure uses Bangladeshi
lakh-crore digit grouping ("22,10,000.00" = 2,210,000.00).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.html_table_row  # noqa: F401
from fetchers.base import FetchResult
from parsers.registry import get_parser

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_V3 = REPO_ROOT / "config" / "sources-v3.json"

# The rebuilt portal's total row, markup-faithful: tfoot, a colspan'd label
# cell, and lakh-crore grouping. Captured live from index.php/tbill.
_NEW_GSOM_HTML = """
<html><body>
<table id="tbill_table">
  <thead>
    <tr><th>Sl. No.</th><th>ISIN</th><th>SecuritiesName</th>
        <th>MarketYield</th><th>OutstandingBDT (in Mill)</th></tr>
  </thead>
  <tbody>
    <tr><td>85</td><td>BD0936409269</td><td>182D T-Bill 01/03/2027</td>
        <td>8.8199</td><td> 20,000.00 </td></tr>
    <tr><td>86</td><td>BD0936409277</td><td>364D T-Bill 30/08/2027</td>
        <td>8.8893</td><td> 20,000.00 </td></tr>
  </tbody>
  <tfoot>
    <tr class="footer-total">
      <td colspan="10" style="text-align:right">Total Outstanding Balance:</td>
      <td>22,10,000.00</td>
    </tr>
  </tfoot>
</table>
</body></html>
"""


def _indicators() -> dict[str, dict]:
    return {i["id"]: i for i in json.loads(SOURCES_V3.read_text())["indicators"]}


class TestRepointedUrls:
    @pytest.mark.parametrize(
        "indicator_id, expected_url",
        [
            ("treasury_bill_outstanding", "https://gsom.bb.org.bd/index.php/tbill"),
            ("treasury_bond_outstanding", "https://gsom.bb.org.bd/index.php/tbond"),
        ],
    )
    def test_points_at_the_rebuilt_portal(self, indicator_id, expected_url):
        assert _indicators()[indicator_id]["fetch"]["url"] == expected_url

    def test_no_indicator_still_uses_a_dead_gsom_php_route(self):
        """The whole `*.php` route family is gone, not just these two -- so
        guard the registry rather than the two ids, in case another
        indicator is ever pointed at the old-style paths."""
        stale = [
            i["id"]
            for i in _indicators().values()
            if "gsom.bb.org.bd" in i.get("fetch", {}).get("url", "")
            and i["fetch"]["url"].endswith(".php")
        ]
        assert stale == []

    def test_the_deterministic_instruction_is_unchanged(self):
        """The repoint is a URL move, NOT a re-parse. If a future change
        needs a different instruction here, that is a signal the total row
        itself moved and this fixture needs re-capturing."""
        for indicator_id in ("treasury_bill_outstanding", "treasury_bond_outstanding"):
            ind = _indicators()[indicator_id]
            assert ind["fetch"]["task"] == "row=Total Outstanding Balance col=2"
            assert ind["parse"]["deterministic"] == "html_table_row"


class TestNewPortalMarkup:
    @pytest.fixture
    def artifact(self, tmp_path: Path) -> FetchResult:
        p = tmp_path / "tbill.html"
        p.write_text(_NEW_GSOM_HTML)
        return FetchResult(
            indicator_id="treasury_bill_outstanding",
            artifact_path=p,
            artifact_type="html",
            fetched_at=datetime.now(timezone.utc),
            source_url="https://gsom.bb.org.bd/index.php/tbill",
            sha256="x" * 64,
            cache_hit=False,
        )

    def test_reads_the_tfoot_total_through_the_colspan_label(self, artifact):
        """`html_table_row` matches on the row's FIRST cell and takes cell
        `col`. On the rebuilt page the first cell is the colspan'd label and
        the second is the figure, so the existing `col=2` still lands on the
        total -- the point of the whole repoint being config-only."""
        r = get_parser("html_table_row").parse(
            artifact, instruction="row=Total Outstanding Balance col=2"
        )
        assert r.value == 2_210_000.0
        assert r._parse_strategy == "html_table_row"

    def test_lakh_crore_grouping_is_not_misread(self, artifact):
        """"22,10,000.00" is 2.21 million, not 22.1 -- Bangladeshi grouping
        puts separators every two digits above the thousand. A naive
        thousands-assumption parser would be off by 10x here, so pin it."""
        r = get_parser("html_table_row").parse(
            artifact, instruction="row=Total Outstanding Balance col=2"
        )
        # In BDT millions, per the table's own "OutstandingBDT (in Mill)"
        # column header -- about 2.21 trillion taka of bills outstanding.
        assert 1_000_000.0 < r.value < 10_000_000.0

    def test_value_stays_inside_the_registry_range(self, artifact):
        r = get_parser("html_table_row").parse(
            artifact, instruction="row=Total Outstanding Balance col=2"
        )
        low, high = _indicators()["treasury_bill_outstanding"]["parse"]["valid_range"]
        assert low <= r.value <= high
