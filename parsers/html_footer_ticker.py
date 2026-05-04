"""Parser for the bb.org.bd footer ticker (policy rate, SLF, SDF, USD/BDT)
and news-article pages (TBS, Daily Star NBR reports).

The instruction names a label (e.g. "Policy Rate"). We find it in the rendered
HTML text and grab the numeric token immediately after.

source_as_of: for news-article pages (TBS, Daily Star), the parser reads the
``<meta property="article:published_time" content="...">`` tag and converts it
to a date. This gives the Brief an accurate freshness signal for slow-cadence
NBR report metrics instead of showing today's EconDelta run date.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# Matches <meta property="article:published_time" content="2026-04-15T06:30:00+06:00" />
# or the plain date form "2026-03-20". Group 1 captures the content value.
_ARTICLE_DATE_RE = re.compile(
    r'<meta\s[^>]*property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _extract_article_date(html: str) -> date | None:
    """Return the article publication date from an HTML meta tag, or None."""
    m = _ARTICLE_DATE_RE.search(html)
    if not m:
        return None
    raw = m.group(1).strip()
    # Try ISO 8601 datetime first (most news sites), then date-only
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(fmt.replace("%z", "+0000"))], fmt).date()
        except ValueError:
            pass
    # dateutil-style: parse the leading YYYY-MM-DD portion directly
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


@register("html_footer_ticker")
class HtmlFooterTickerParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        text = artifact.artifact_path.read_text()
        source_as_of = _extract_article_date(text)
        plain = re.sub(r"<[^>]+>", " ", text)
        pattern = re.escape(instruction) + r"\s*([0-9]+(?:\.[0-9]+)?)\s*%?"
        m = re.search(pattern, plain, re.IGNORECASE)
        if not m:
            raise ParseError(f"label {instruction!r} not found in HTML")
        return ParseResult(
            value=float(m.group(1)),
            _parse_strategy="html_footer_ticker",
            source_as_of=source_as_of,
        )
