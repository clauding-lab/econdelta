"""Off-box export of irreplaceable history (E2.4).

``data/`` is git-ignored and exists only on ExonVPS; ``metric_history_monthly``'s
hand-verified fiscal backfill (landmine 32) and the LLM-extracted / static-tier
history in ``metric_history`` are NOT re-scrapable — Supabase is the single
off-box copy, so a Supabase loss would be a permanent data loss. This exports
those tables to a portable, timestamped JSON file so the history survives.

Designed to run OFF the box that holds the only other copy (i.e. cron it on
Hetzner, or point ``--out-dir`` at a git-tracked path for a committed snapshot).
The re-scrapable daily market series (DSE index/tickers, forex, commodity) are
lower priority — included by default (a fuller backup never hurts) but droppable
with ``--irreplaceable-only`` for a lean, committable snapshot.

Usage:
    python -m scripts.export_history --out-dir /var/backups/econdelta
    python -m scripts.export_history --out-dir docs/snapshots --irreplaceable-only

Reads with the Supabase key in the environment (SUPABASE_SERVICE_ROLE_KEY on the
box; the anon key also works for the anon-readable tables). See docs/backup-export.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

logger = logging.getLogger("export_history")

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_V3_PATH = REPO_ROOT / "config" / "sources-v3.json"

_PAGE_SIZE = 1000
_TIMEOUT = 60

# Tables to export. metric_history_monthly is the fiscal backfill; metric_history
# is the daily backend (its slow-cadence rows are the LLM/static tier).
_TABLES = ("metric_history_monthly", "metric_history")

# Scraper-produced daily-market ids with no sources-v3.json cadence — re-scrapable
# (the source republishes them every trading day), so --irreplaceable-only drops
# them alongside config daily ids and the dse_close_/dse_sector_heat_ prefixes.
_SCRAPER_DAILY_IDS = frozenset({
    "dsex", "ds30", "dses", "dsex_change", "dsex_change_pct",
    "turnover_crore", "total_trades", "advancing", "declining", "unchanged",
    "usd_bdt_mid", "usd_bdt_buy", "usd_bdt_sell", "eur_bdt", "gbp_bdt",
    "gross_reserves_usd_bn", "import_cover_months",
    "usd_bdt_exchange_rate", "fx_reserve_gross_and_bpm6",
})


class ExportError(RuntimeError):
    """Raised when the export cannot complete (missing creds or a read failure)."""


def _resolve_credentials(url: str | None, key: str | None) -> tuple[str, str]:
    resolved_url = url or os.environ.get("SUPABASE_URL")
    resolved_key = (
        key
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SERVICE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not resolved_url:
        raise ExportError("SUPABASE_URL not set in env or --url")
    if not resolved_key:
        raise ExportError("no Supabase key set (SUPABASE_SERVICE_ROLE_KEY / --key)")
    return resolved_url.rstrip("/"), resolved_key


def paginate_table(
    table: str, *, url: str | None = None, key: str | None = None,
    session: requests.Session | None = None, page_size: int = _PAGE_SIZE,
) -> list[dict]:
    """Return every row of ``table`` (all columns), paging past PostgREST's cap."""
    base_url, resolved_key = _resolve_credentials(url, key)
    headers = {"apikey": resolved_key, "Authorization": f"Bearer {resolved_key}"}
    sess = session or requests.Session()
    rows: list[dict] = []
    offset = 0
    while True:
        endpoint = (
            f"{base_url}/rest/v1/{table}"
            f"?select=*&order=metric_id.asc,as_of.asc&limit={page_size}&offset={offset}"
        )
        try:
            resp = sess.get(endpoint, headers=headers, timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise ExportError(f"read {table} failed: {e}") from e
        if resp.status_code not in (200, 206):
            raise ExportError(f"read {table} HTTP {resp.status_code}: {resp.text[:200]}")
        page = resp.json()
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return rows


def _daily_config_ids(config_path: Path = SOURCES_V3_PATH) -> frozenset[str]:
    try:
        cfg = json.loads(Path(config_path).read_text())
    except (OSError, json.JSONDecodeError):
        return frozenset()
    return frozenset(
        ind["id"] for ind in cfg.get("indicators", []) if ind.get("cadence") == "daily"
    )


def is_rescrapable_daily(metric_id: str, daily_config_ids: frozenset[str]) -> bool:
    """True for a daily market series the source republishes (safe to skip in a
    lean snapshot). Best-effort — the default full export keeps everything."""
    if metric_id in daily_config_ids or metric_id in _SCRAPER_DAILY_IDS:
        return True
    return metric_id.startswith(("dse_close_", "dse_sector_heat_"))


def export_history(
    out_dir: Path, *, url: str | None = None, key: str | None = None,
    irreplaceable_only: bool = False,
    fetcher: Callable[[str], list[dict]] | None = None,
    now: datetime | None = None,
    config_path: Path = SOURCES_V3_PATH,
) -> Path:
    """Fetch both history tables and write one timestamped JSON export.

    Args:
        out_dir: directory to write into (created if missing).
        fetcher: table→rows callable (default: live PostgREST). Injected in tests.
        irreplaceable_only: drop re-scrapable daily-market ids from metric_history.

    Returns the written file path. Raises ExportError on a read/credential failure.
    """
    fetch = fetcher or (lambda table: paginate_table(table, url=url, key=key))
    stamp = (now or datetime.now(timezone.utc))
    daily_ids = _daily_config_ids(config_path) if irreplaceable_only else frozenset()

    tables: dict[str, list[dict]] = {}
    for table in _TABLES:
        rows = fetch(table)
        if table == "metric_history" and irreplaceable_only:
            rows = [r for r in rows if not is_rescrapable_daily(r.get("metric_id", ""), daily_ids)]
        tables[table] = rows
        logger.info("exported %d rows from %s", len(rows), table)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"econdelta_history_export_{stamp.strftime('%Y-%m-%d')}.json"
    payload = {
        "exported_at": stamp.isoformat(),
        "tier": "irreplaceable_only" if irreplaceable_only else "all",
        "manifest": {t: len(rows) for t, rows in tables.items()},
        "tables": tables,
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("wrote %s (%s)", out_path, payload["manifest"])
    return out_path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Off-box export of irreplaceable EconDelta history (E2.4)")
    p.add_argument("--out-dir", type=Path, default=Path("exports"))
    p.add_argument("--irreplaceable-only", action="store_true",
                   help="drop re-scrapable daily market series (lean, committable snapshot)")
    p.add_argument("--url", type=str, default=None)
    p.add_argument("--key", type=str, default=None)
    args = p.parse_args(argv)
    try:
        export_history(
            args.out_dir, url=args.url, key=args.key,
            irreplaceable_only=args.irreplaceable_only,
        )
    except ExportError as e:
        logger.error("export failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
