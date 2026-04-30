import pytest
from fetchers.pdf_discovery import discover_latest_pdf_link

_FIXTURE_HTML = """
<html><body>
<table>
  <tr><td>March 2026</td><td><a href="/files/pub/3-11/march-2026.pdf">Download</a></td></tr>
  <tr><td>February 2026</td><td><a href="/files/pub/3-11/feb-2026.pdf">Download</a></td></tr>
  <tr><td>April 2026</td><td><a href="/files/pub/3-11/april-2026.pdf">Download</a></td></tr>
</table>
</body></html>
"""


def test_discover_latest_pdf_picks_most_recent_month():
    base = "https://www.bb.org.bd"
    link = discover_latest_pdf_link(html=_FIXTURE_HTML, base_url=base)
    assert link == f"{base}/files/pub/3-11/april-2026.pdf"


def test_discover_latest_pdf_raises_on_empty():
    with pytest.raises(ValueError, match="no.*pdf"):
        discover_latest_pdf_link(html="<html></html>", base_url="https://example.com")
