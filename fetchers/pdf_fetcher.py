"""HTTP fetcher for PDFs with sha256 dedup."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pdfplumber

from fetchers.base import FetchError, FetchResult


def _derive_filename(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name or "download.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def fetch_pdf(*, url: str, indicator_id: str, snapshot_dir: Path, as_of_month: str) -> FetchResult:
    out_dir = snapshot_dir / "_pdfs" / indicator_id / as_of_month
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _derive_filename(url)

    try:
        req = Request(url, headers={"User-Agent": "EconDelta/3.0"})
        with urlopen(req, timeout=60) as resp:
            body = resp.read()
    except Exception as e:
        raise FetchError(f"PDF download failed for {url}: {e}") from e

    sha = hashlib.sha256(body).hexdigest()
    cache_hit = out_path.exists() and hashlib.sha256(out_path.read_bytes()).hexdigest() == sha
    if not cache_hit:
        out_path.write_bytes(body)
        page_count = _safe_page_count(out_path)
        sidecar = {
            "source_url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sha256": sha,
            "page_count": page_count,
            "byte_size": len(body),
        }
        out_path.with_suffix(".meta.json").write_text(json.dumps(sidecar, indent=2))

    return FetchResult(
        indicator_id=indicator_id,
        artifact_path=out_path,
        artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url=url,
        sha256=sha,
        cache_hit=cache_hit,
    )


def _safe_page_count(path: Path) -> int:
    try:
        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0
