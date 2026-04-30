"""Stage 1 entry point: walk sources-v3.json, fetch every due indicator,
write artifacts under data/_pdfs/ and data/_html/.

Usage:
    python fetch_all.py [--dry-run] [--config config/sources-v3.json] [--only INDICATOR_ID]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from fetchers.base import FetchError, FetchResult
from fetchers.html_fetcher import fetch_html
from fetchers.pdf_discovery import discover_latest_pdf_link
from fetchers.pdf_fetcher import fetch_pdf

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "sources-v3.json"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"

logger = logging.getLogger("fetch_all")


def _download_index_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": "EconDelta/3.0"})
    with urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def _fetch_one(indicator: dict, data_root: Path) -> FetchResult | None:
    fetch_block = indicator["fetch"]
    indicator_id = indicator["id"]
    if fetch_block["type"] == "html":
        return fetch_html(
            url=fetch_block["url"],
            indicator_id=indicator_id,
            snapshot_dir=data_root / "_html" / indicator_id,
        )
    if fetch_block["type"] == "pdf":
        url = fetch_block["url"]
        if fetch_block.get("discover") == "latest_pdf_link":
            html = _download_index_html(url)
            url = discover_latest_pdf_link(html=html, base_url=url)
        as_of_month = datetime.now(timezone.utc).strftime("%Y-%m")
        return fetch_pdf(
            url=url,
            indicator_id=indicator_id,
            snapshot_dir=data_root,
            as_of_month=as_of_month,
        )
    logger.warning("unsupported fetch.type=%s for %s", fetch_block.get("type"), indicator_id)
    return None


def run(*, config_path: Path, data_root: Path, only: str | None = None, dry_run: bool = False) -> list[FetchResult]:
    cfg = json.loads(config_path.read_text())
    results: list[FetchResult] = []
    for ind in cfg["indicators"]:
        if only and ind["id"] != only:
            continue
        if dry_run:
            logger.info("[dry-run] would fetch %s (%s)", ind["id"], ind["fetch"]["type"])
            continue
        try:
            r = _fetch_one(ind, data_root)
        except FetchError as e:
            logger.error("fetch_failed: %s — %s", ind["id"], e)
            continue
        if r:
            results.append(r)
            logger.info("fetched %s sha=%s cache_hit=%s", r.indicator_id, r.sha256[:8], r.cache_hit)
    return results


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--only", type=str, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    results = run(config_path=args.config, data_root=args.data_root, only=args.only, dry_run=args.dry_run)
    cache_hits = sum(1 for r in results if r.cache_hit)
    print(f"Fetched: {len(results)} · Cache hits: {cache_hits} · Failed: see log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
