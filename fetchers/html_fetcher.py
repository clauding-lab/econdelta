"""Playwright-stealth wrapper for HTML fetching.

Reuses the stealth context pattern from scrapers/bb_forex.py. Persists
rendered HTML to <snapshot_dir>/<YYYY-MM-DD>.html and detects
content-unchanged via sha256 across runs.

For Akamai/Radware-protected pages (www.bb.org.bd/en/...), the first visit
returns a 313-byte challenge page that runs JS to set TSPD cookies; the
second visit returns the real content. We detect the challenge HTML
('Pardon' or 'support ID is:') and reload once to absorb it.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from fetchers.base import FetchError, FetchResult

logger = logging.getLogger("html_fetcher")

# Markers in the BB Akamai challenge HTML (~313 bytes when stripped of tags).
_CHALLENGE_MARKERS: tuple[str, ...] = ("Pardon Our Interruption", "support ID is:")

# Page.goto timeout — same value bb_forex.py uses after the 45s→90s bump
# absorbed the dawn-hour latency on Sundays.
_DEFAULT_TIMEOUT_MS = 90_000

# Time for Akamai challenge JS to run + set TSPD cookies on first visit.
_CHALLENGE_SETTLE_MS = 10_000

# Time after reload for the real page to render before reading content().
_RELOAD_SETTLE_MS = 8_000


def _is_challenge(html: str) -> bool:
    return any(marker in html for marker in _CHALLENGE_MARKERS)


def fetch_html(
    *,
    url: str,
    indicator_id: str,
    snapshot_dir: Path,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
) -> FetchResult:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = snapshot_dir / f"{today}.html"

    # File-URL test fixtures don't need stealth/settle/reload.
    is_file_url = url.startswith("file://")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="Asia/Dhaka",
            )
            page = context.new_page()
            Stealth().apply_stealth_sync(page)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            if not is_file_url:
                page.wait_for_timeout(_CHALLENGE_SETTLE_MS)

            html = page.content()

            if not is_file_url and _is_challenge(html):
                logger.warning(
                    "challenge detected on first visit for %s — reloading after cookie set",
                    indicator_id,
                )
                page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(_RELOAD_SETTLE_MS)
                html = page.content()
                if _is_challenge(html):
                    logger.error(
                        "challenge persisted after reload for %s — saving challenge page; downstream will needs_review",
                        indicator_id,
                    )
            browser.close()
    except Exception as e:
        raise FetchError(f"playwright fetch failed for {url}: {e}") from e

    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    cache_hit = out_path.exists() and (
        hashlib.sha256(out_path.read_bytes()).hexdigest() == sha
    )
    if not cache_hit:
        out_path.write_text(html)
    return FetchResult(
        indicator_id=indicator_id,
        artifact_path=out_path,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url=url,
        sha256=sha,
        cache_hit=cache_hit,
    )
