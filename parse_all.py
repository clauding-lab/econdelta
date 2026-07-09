"""Stage 2 entry point: walk sources-v3.json, parse each fetched artifact,
emit per-indicator snapshots."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import parsers.dam_ticker  # noqa: F401
import parsers.dse_sector_heat  # noqa: F401
import parsers.html_call_money  # noqa: F401

# Auto-import all parser modules so they register
import parsers.html_footer_ticker  # noqa: F401
import parsers.html_table_row  # noqa: F401
import parsers.pdf_component  # noqa: F401
import parsers.pdf_fsr_ownership_cluster  # noqa: F401
import parsers.pdf_mfr_row  # noqa: F401
import parsers.pdf_table_column_latest  # noqa: F401
import parsers.pdf_table_latest  # noqa: F401
import parsers.pdf_table_row  # noqa: F401
import parsers.pdf_table_total  # noqa: F401
from fetchers.base import FetchResult, parse_period
from parsers.hybrid import parse_one
from utils.floor import assess_parse_floor
from utils.notifier import notify

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "sources-v3.json"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"

logger = logging.getLogger("parse_all")


def _read_sidecar_period(pdf: Path) -> tuple[int, int] | None:
    """The (year, month) issue period recorded in a PDF's .meta.json sidecar.

    Returns None for a legacy dir whose sidecar predates the period field, a
    missing/unreadable/hand-broken sidecar — the caller then falls back to mtime.
    """
    meta = pdf.with_suffix(".meta.json")
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text())
    except (OSError, ValueError):
        return None
    return parse_period(data.get("period"))


def _pdf_recency_key(pdf: Path) -> tuple[bool, tuple[int, int], float]:
    """Sort key selecting the newest ISSUE among the PDFs in one month-dir.

    Primary: the recorded (year, month) period — a PDF with a period always
    outranks a legacy period-less one, and among periods the later issue wins.
    Fallback / tiebreak: file mtime (all a legacy sidecar-less dir has).
    """
    period = _read_sidecar_period(pdf)
    return (period is not None, period or (0, 0), pdf.stat().st_mtime)


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
        # A month-dir accumulates >1 PDF when a NEW source issue drops mid-month:
        # discovery returns whatever is latest each day, and fetch_pdf writes by
        # CURRENT month, so successive issues land in the same dir (observed on
        # ExonVPS: 2026-06/ held BOTH 2026_april.pdf AND 2026_may.pdf). glob()
        # order is arbitrary, so a bare pdfs[0] could read the STALE issue — this
        # is how the pipeline "held April 2026 while BB published May 2026"
        # (E1 leftover). Pick the newest ISSUE by the (year, month) period each
        # artifact recorded in its .meta.json sidecar at fetch time — immune to
        # both mtime races (a re-fetch/rsync rewriting a stale file's mtime,
        # landmine 13) and filename drift. mtime is only the tiebreak and the
        # fallback for legacy dirs whose sidecars predate the period field. A
        # stable single-PDF month is unaffected.
        artifact_path = max(pdfs, key=_pdf_recency_key)
        if len(pdfs) > 1:
            logger.info(
                "%s: %d PDFs in %s — parsing newest issue (%s)",
                indicator_id, len(pdfs), month_dirs[0].name, artifact_path.name,
            )
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


_PREFLIGHT_MAX_ATTEMPTS = 3
_PREFLIGHT_BACKOFF_SEC = (5, 15)


def _claude_warmup() -> None:
    """Force claude OAuth token refresh ahead of the strict preflight.

    Systemd-context auto-refresh of claude's cached access_token has been
    observed to stall while interactive invocations refresh cleanly,
    leaving the strict preflight to fail with 401. A throwaway --print
    call without --strict-mcp-config reliably rewrites ~/.claude/.credentials.json,
    so the strict preflight that follows sees a fresh token. Best-effort:
    real validation lives in _claude_preflight() — failures are absorbed.
    """
    binary = os.environ.get("CLAUDE_BINARY", "claude")
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            [binary, "--print", "ping"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        logger.info("claude warmup skipped: %s", e)
        return
    elapsed = time.monotonic() - t0
    logger.info("claude warmup exit=%d in %.1fs", result.returncode, elapsed)


def _claude_preflight() -> bool:
    """Verify subscription claude CLI is reachable before running hybrid extraction.

    Without this, a flaked claude (expired OAuth, network blip, etc.) causes every
    indicator to fall through to needs_review with empty values — the service still
    exits 0 and downstream aggregate happily writes a mostly-empty latest.json. By
    failing fast here, systemd marks parse.service failed → parse-retry.timer can
    take over → on-call (or human eyeball) sees the failure.

    Retries up to _PREFLIGHT_MAX_ATTEMPTS times with backoff to absorb transient
    edge-side blips (e.g. Anthropic API contention during the cron window). Each
    attempt logs stdout, stderr, exit code, and elapsed time so future failures
    are diagnosable without SSH'ing the host.
    """
    binary = os.environ.get("CLAUDE_BINARY", "claude")
    # --strict-mcp-config blocks MCP-plugin loading. Without it, any
    # plugin installed in ~/.claude/plugins/ (e.g. discord-vps-setup
    # on Hetzner) can hijack stdout and make the CLI exit 1 with empty
    # stderr. Mirrors the fix in claude_max/max_client.py (e027106).
    cmd = [binary, "--print", "--strict-mcp-config", "--model", "claude-opus-4-8"]
    for attempt in range(1, _PREFLIGHT_MAX_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                input="say ok",
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            elapsed = time.monotonic() - t0
            logger.error(
                "claude pre-flight attempt %d/%d crashed after %.1fs: %s",
                attempt, _PREFLIGHT_MAX_ATTEMPTS, elapsed, e,
            )
        else:
            elapsed = time.monotonic() - t0
            if result.returncode == 0:
                if attempt > 1:
                    logger.info(
                        "claude pre-flight ok on attempt %d/%d (%.1fs)",
                        attempt, _PREFLIGHT_MAX_ATTEMPTS, elapsed,
                    )
                return True
            logger.error(
                "claude pre-flight attempt %d/%d exited %d after %.1fs — stdout=%r stderr=%r",
                attempt, _PREFLIGHT_MAX_ATTEMPTS, result.returncode, elapsed,
                result.stdout.strip()[:200], result.stderr.strip()[:200],
            )
        if attempt < _PREFLIGHT_MAX_ATTEMPTS:
            backoff = _PREFLIGHT_BACKOFF_SEC[min(attempt - 1, len(_PREFLIGHT_BACKOFF_SEC) - 1)]
            logger.info("claude pre-flight retry in %ds", backoff)
            time.sleep(backoff)
    return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--only", type=str, default=None)
    p.add_argument("--skip-claude-preflight", action="store_true",
                   help="Skip claude reachability check (for tests / deterministic-only runs)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if not args.skip_claude_preflight:
        _claude_warmup()
        if not _claude_preflight():
            logger.error("aborting parse run — claude CLI not reachable; aggregate will keep last good latest.json")
            return 1
    snapshots = run(config_path=args.config, data_root=args.data_root, only=args.only)
    by_prov: dict[str, int] = {}
    for s in snapshots:
        by_prov[s.get("_provenance", "unknown")] = by_prov.get(s.get("_provenance", "unknown"), 0) + 1
    print(f"Parsed: {len(snapshots)} ({', '.join(f'{k}:{v}' for k, v in by_prov.items())})")

    # E2.5 deterministic floor — fires when parse produced almost nothing
    # (systemic parse break, or a fetch outage that left no artifacts), BEFORE
    # and independent of the LLM review. Skipped for --only (targeted debug).
    if not args.only:
        cfg = json.loads(args.config.read_text())
        verdict = assess_parse_floor(due=len(cfg.get("indicators", [])), produced=len(snapshots))
        if verdict.breached:
            logger.error("parse floor breached: %s", verdict.reason)
            notify("error", "parse floor breached", verdict.reason)
    return 0


if __name__ == "__main__":
    import sys

    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("parse", "econdelta-parse.service", main))
