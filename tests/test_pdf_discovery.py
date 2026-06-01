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


def test_discover_handles_quarter_range_with_comma():
    """BB's FSAR index uses 'July-September, 2025' (comma between month and year)
    instead of the 'September 2025' format. The discovery regex must accept both."""
    base = "https://www.bb.org.bd"
    html = """
    <html><body><table>
      <tr><td>April-June, 2025</td><td><a href="/pub/q/q2-2025.pdf">x</a></td></tr>
      <tr><td>July-September, 2025</td><td><a href="/pub/q/q3-2025.pdf">x</a></td></tr>
      <tr><td>January-March, 2024</td><td><a href="/pub/q/q1-2024.pdf">x</a></td></tr>
    </table></body></html>
    """
    link = discover_latest_pdf_link(html=html, base_url=base)
    assert link == f"{base}/pub/q/q3-2025.pdf"
