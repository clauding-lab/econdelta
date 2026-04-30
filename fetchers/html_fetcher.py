"""Playwright-stealth wrapper for HTML fetching.

Reuses the stealth context pattern from scrapers/bb_forex.py. Persists
rendered HTML to <snapshot_dir>/<YYYY-MM-DD>.html and detects
content-unchanged via sha256 across runs.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from fetchers.base import FetchError, FetchResult


def fetch_html(*, url: str, indicator_id: str, snapshot_dir: Path) -> FetchResult:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = snapshot_dir / f"{today}.html"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            html = page.content()
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
