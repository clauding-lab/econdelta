"""Stage 2 entry point: walk sources-v3.json, parse each fetched artifact,
emit per-indicator snapshots."""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fetchers.base import FetchResult
from parsers.hybrid import parse_one

# Auto-import all parser modules so they register
import parsers.html_footer_ticker  # noqa: F401
import parsers.html_table_row  # noqa: F401
import parsers.html_call_money  # noqa: F401
import parsers.pdf_component  # noqa: F401
import parsers.pdf_table_row  # noqa: F401
import parsers.pdf_table_total  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "sources-v3.json"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"

logger = logging.getLogger("parse_all")


def _load_artifact_for(indicator: dict, data_root: Path) -> FetchResult | None:
    indicator_id = indicator["id"]
    fetch_type = indicator["fetch"]["type"]
    if fetch_type == "html":
        d = data_root / "_html" / indicator_id
        if not d.exists():
            return None
        candidates = sorted(d.glob("*.html"), reverse=True)
        if not candidates:
            return None
        artifact_path = candidates[0]
    elif fetch_type == "pdf":
        d = data_root / "_pdfs" / indicator_id
        if not d.exists():
            return None
        month_dirs = sorted([p for p in d.iterdir() if p.is_dir()], reverse=True)
        if not month_dirs:
            return None
        pdfs = list(month_dirs[0].glob("*.pdf"))
        if not pdfs:
            return None
        artifact_path = pdfs[0]
    else:
        return None
    return FetchResult(
        indicator_id=indicator_id,
        artifact_path=artifact_path,
        artifact_type=fetch_type,
        fetched_at=datetime.fromtimestamp(artifact_path.stat().st_mtime, tz=timezone.utc),
        source_url=indicator["fetch"]["url"],
        sha256="0" * 64,
        cache_hit=False,
    )


def _emit_snapshot(snapshot: dict, data_root: Path) -> Path:
    out_dir = data_root / snapshot["indicator_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = out_dir / f"{today}.json"
    path.write_text(json.dumps(snapshot, indent=2, default=str))
    return path


def _load_history(indicator_id: str, data_root: Path, n: int = 3) -> list[float]:
    d = data_root / indicator_id
    if not d.exists():
        return []
    paths = sorted(d.glob("*.json"), reverse=True)[1 : n + 1]
    out: list[float] = []
    for p in paths:
        try:
            v = json.loads(p.read_text()).get("value")
            if isinstance(v, (int, float)):
                out.append(float(v))
        except json.JSONDecodeError:
            continue
    return out


def run(*, config_path: Path, data_root: Path, only: str | None = None) -> list[dict]:
    cfg = json.loads(config_path.read_text())
    snapshots: list[dict] = []
    for ind in cfg["indicators"]:
        if only and ind["id"] != only:
            continue
        artifact = _load_artifact_for(ind, data_root)
        if artifact is None:
            logger.warning("no artifact for %s — skipping", ind["id"])
            continue
        history = _load_history(ind["id"], data_root)
        try:
            snapshot = parse_one(artifact, ind, history=history)
        except Exception as e:
            logger.error("parse_one raised for %s: %s", ind["id"], e)
            continue
        _emit_snapshot(snapshot, data_root)
        snapshots.append(snapshot)
        logger.info("parsed %s value=%s provenance=%s", ind["id"], snapshot.get("value"), snapshot.get("_provenance"))
    return snapshots


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--only", type=str, default=None)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    snapshots = run(config_path=args.config, data_root=args.data_root, only=args.only)
    by_prov: dict[str, int] = {}
    for s in snapshots:
        by_prov[s.get("_provenance", "unknown")] = by_prov.get(s.get("_provenance", "unknown"), 0) + 1
    print(f"Parsed: {len(snapshots)} ({', '.join(f'{k}:{v}' for k, v in by_prov.items())})")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
