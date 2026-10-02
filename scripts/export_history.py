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

Repair snapshot (round 4, owner decision D2): service key only, writers paused, new DIR:
    python -m scripts.export_history --repair-snapshot DIR --table metric_history
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

logger = logging.getLogger("export_history")

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_V3_PATH = REPO_ROOT / "config" / "sources-v3.json"

_PAGE_SIZE = 1000
_TIMEOUT = 60

# Tables to export. metric_history_monthly is the fiscal backfill; metric_history
# is the daily backend (its slow-cadence rows are the LLM/static tier).
_TABLE_KEYS = {
    "metric_history_monthly": ("metric_id", "as_of"),
    "metric_history": ("metric_id", "as_of"),
    "metric_definitions": ("metric_id",),
    "metric_definitions_monthly": ("metric_id",),
    "auction_results": ("auction_date", "tenor"),
    "media_review": ("id",),
}
_TABLES = tuple(_TABLE_KEYS)

# Scraper-produced daily-market ids with no sources-v3.json cadence — re-scrapable
# (the source republishes them every trading day), so --irreplaceable-only drops
# them alongside config daily ids and the dse_close_/dse_sector_heat_ prefixes.
_SCRAPER_DAILY_IDS = frozenset(
    {
        "dsex",
        "ds30",
        "dses",
        "dsex_change",
        "dsex_change_pct",
        "turnover_crore",
        "total_trades",
        "advancing",
        "declining",
        "unchanged",
        "usd_bdt_mid",
        "usd_bdt_buy",
        "usd_bdt_sell",
        "eur_bdt",
        "gbp_bdt",
        "gross_reserves_usd_bn",
        "import_cover_months",
        "usd_bdt_exchange_rate",
        "fx_reserve_gross_and_bpm6",
    }
)


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
    table: str,
    *,
    url: str | None = None,
    key: str | None = None,
    session: requests.Session | None = None,
    page_size: int = _PAGE_SIZE,
) -> list[dict]:
    """Read a stable table past PostgREST's cap; this is not a snapshot.

    Pause every overlapping writer through final repair read-back. Concurrent
    deletes can shift offsets and omit rows. A routine concurrent backup needs
    a transactionally consistent snapshot; validate final counts and keys.
    """
    if table not in _TABLE_KEYS or page_size < 1:
        raise ExportError("unsupported table or invalid page size")
    base_url, resolved_key = _resolve_credentials(url, key)
    headers = {"apikey": resolved_key, "Authorization": f"Bearer {resolved_key}"}
    sess = session or requests.Session()
    rows: list[dict] = []
    offset = 0
    seen: set[tuple] = set()
    order = ",".join(f"{column}.asc" for column in _TABLE_KEYS[table])
    while True:
        endpoint = (
            f"{base_url}/rest/v1/{table}?select=*&order={order}&limit={page_size}&offset={offset}"
        )
        try:
            resp = sess.get(endpoint, headers=headers, timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise ExportError(f"read {table} failed: {e}") from e
        if resp.status_code not in (200, 206):
            raise ExportError(f"read {table} HTTP {resp.status_code}: {resp.text[:200]}")
        page = resp.json()
        if not isinstance(page, list):
            raise ExportError(f"read {table}: expected a row list")
        if not page:
            break
        for row in page:
            if not isinstance(row, dict) or any(row.get(k) is None for k in _TABLE_KEYS[table]):
                raise ExportError(f"read {table}: missing row key")
            identity = tuple(row[k] for k in _TABLE_KEYS[table])
            if identity in seen:
                raise ExportError(f"read {table}: duplicate key / pagination made no progress")
            seen.add(identity)
        rows.extend(page)
        # The server's row cap can be lower than the requested page size.
        # For a stable table, stop on EMPTY; advance by rows actually returned.
        offset += len(page)
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
    out_dir: Path,
    *,
    url: str | None = None,
    key: str | None = None,
    irreplaceable_only: bool = False,
    fetcher: Callable[[str], list[dict]] | None = None,
    now: datetime | None = None,
    config_path: Path = SOURCES_V3_PATH,
) -> Path:
    """Fetch history, definitions and supporting evidence into one JSON export.

    Args:
        out_dir: directory to write into (created if missing).
        fetcher: table→rows callable (default: live PostgREST). Injected in tests.
        irreplaceable_only: drop re-scrapable daily-market ids from metric_history.

    Returns the written file path. Raises ExportError on a read/credential failure.
    """
    fetch = fetcher or (lambda table: paginate_table(table, url=url, key=key))
    stamp = now or datetime.now(timezone.utc)
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
    # Atomic write (.tmp → os.replace, the aggregate_latest.write_latest
    # pattern). The filename is DATE-based, so a same-day re-run targets the
    # SAME path — a direct write_text that crashes mid-write (disk full, OOM,
    # SIGKILL) would leave truncated invalid JSON where a good backup used to
    # be, destroying the very copy this job exists to protect. Writing to a
    # sibling .tmp and os.replace-ing means the prior good file survives any
    # crash; the .tmp leftover is removed on the way out.
    tmp_path = out_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, default=str))
        os.replace(tmp_path, out_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    logger.info("wrote %s (%s)", out_path, payload["manifest"])
    return out_path


def _service_key() -> str:
    """The service key only: an RLS-trimmed anon read would pass every check while short."""
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not key:
        raise ExportError(
            "repair snapshot needs SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SERVICE_KEY "
            "(service key only; the anon key is refused)"
        )
    problem = _service_key_problem(key)
    if problem:
        raise ExportError(f"repair snapshot refused: {problem} (service key only)")
    return key


def _service_key_problem(key: str) -> str | None:
    """A JWT must carry role service_role (payload read, signature not checked); an opaque key
    must be an ``sb_secret_`` key. The key itself is never echoed."""
    parts = key.split(".")
    if len(parts) != 3:
        return None if key.startswith("sb_secret_") else (
            "the key is neither a service_role JWT nor an sb_secret_ key")
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return "the key looks like a JWT but its payload does not decode"
    role = payload.get("role") if isinstance(payload, dict) else None
    return None if role == "service_role" else f"the key's JWT role is {role!r}, not service_role"


def _project_ref(url: str | None) -> str:
    host = urlparse(url or os.environ.get("SUPABASE_URL") or "").hostname or ""
    if not host.endswith(".supabase.co") or host.count(".") != 2:
        raise ExportError("SUPABASE_URL must be https://<project>.supabase.co")
    return host.split(".")[0]


def _write_bytes(path: Path, data: bytes) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp_path, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def export_repair_snapshot(
    out_dir: Path,
    tables: tuple[str, ...] = ("metric_history",),
    fetcher: Callable[[str, str], list[dict]] | None = None,
    *,
    url: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Write the split repair layout: ``<table>.json`` per table plus ``manifest.json``.

    Round 4 (owner decision D2, 2 Oct 2026; docs/reviews/2026-09-25-history-repair-manifest.md).
    Read-only toward the database, service key only, never into an existing directory. The read
    is NOT a snapshot (see paginate_table): pause every overlapping writer first.
    ``fetcher(table, key)`` replaces the live PostgREST read in tests.
    """
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise ExportError(f"{out_dir} already exists; a repair snapshot never overwrites evidence")
    unknown = [t for t in tables if t not in _TABLE_KEYS]
    if not tables or unknown:
        raise ExportError(f"unsupported repair snapshot table(s): {unknown or 'none given'}")
    key = _service_key()
    project = _project_ref(url)
    fetch = fetcher or (lambda table, k: paginate_table(table, url=url, key=k))
    started_at = (now or datetime.now(timezone.utc)).isoformat()
    payloads = {table: json.dumps(fetch(table, key), indent=2).encode() for table in tables}
    out_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    refs = {}
    for table, raw in payloads.items():
        _write_bytes(out_dir / f"{table}.json", raw)
        refs[table] = {
            "rows": len(json.loads(raw)),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "key": list(_TABLE_KEYS[table]),
        }
        logger.info("repair snapshot: %d rows from %s", refs[table]["rows"], table)
    manifest = {
        "target_project": project,
        "started_at": started_at,
        "non_transactional": True,
        "key_role": "service",
        "tables": refs,
    }
    _write_bytes(out_dir / "manifest.json", json.dumps(manifest, indent=2).encode())
    return out_dir


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    p = argparse.ArgumentParser(
        description="Off-box export of irreplaceable EconDelta history (E2.4)"
    )
    p.add_argument("--out-dir", type=Path, default=Path("exports"))
    p.add_argument(
        "--irreplaceable-only",
        action="store_true",
        help="drop re-scrapable daily market series (lean, committable snapshot)",
    )
    p.add_argument("--url", type=str, default=None)
    p.add_argument("--key", type=str, default=None)
    p.add_argument(
        "--repair-snapshot",
        type=Path,
        default=None,
        metavar="DIR",
        help="write the split repair layout (<table>.json + manifest.json) into a NEW DIR; "
        "service key from the environment only",
    )
    p.add_argument(
        "--table",
        action="append",
        default=None,
        help="table for --repair-snapshot (repeatable; default metric_history)",
    )
    args = p.parse_args(argv)
    try:
        if args.repair_snapshot is not None:
            if args.key is not None:
                raise ExportError("--repair-snapshot reads the service key from the environment")
            export_repair_snapshot(
                args.repair_snapshot, tuple(args.table or ("metric_history",)), url=args.url
            )
            return 0
        export_history(
            args.out_dir,
            url=args.url,
            key=args.key,
            irreplaceable_only=args.irreplaceable_only,
        )
    except ExportError as e:
        logger.error("export failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
