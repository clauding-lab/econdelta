"""Subprocess wrapper around the `claude -p` Max CLI.

Mirrors brief/claude/max_client.py from the-brief. Adapted for EconDelta:
  - Default model: claude-sonnet-4-6 (the-brief defaults to opus)
  - Default effort: medium (the-brief uses high for headline curation)

No Anthropic API calls. Auth is via the OS user's ~/.claude/.credentials.json
(Max OAuth), injected by the CLI itself — we pass no tokens.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*?)\n```$", re.DOTALL)


def _strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        return m.group(1)
    return stripped if stripped != text else text


class MaxCallError(RuntimeError):
    """Raised when the CLI fails, times out, or returns non-JSON."""


@dataclass(frozen=True)
class MaxCallResult:
    raw_text: str
    parsed: Any | None
    usage: dict[str, Any]
    total_cost_usd: float | None
    duration_s: float = 0.0
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})


def run_max(
    *,
    prompt: str,
    model: str = "claude-sonnet-4-6",
    timeout_s: int = 300,
    claude_binary: str | None = None,
    effort: str = "medium",
) -> MaxCallResult:
    if claude_binary is None:
        claude_binary = os.environ.get("CLAUDE_BINARY", "claude")
    argv = [
        claude_binary, "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
        "--tools", "",
        "--permission-mode", "bypassPermissions",
        "--effort", effort,
    ]
    t0 = time.monotonic()
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as e:
        raise MaxCallError(f"Claude CLI timed out after {timeout_s}s") from e
    except FileNotFoundError as e:
        raise MaxCallError(f"Claude CLI binary not found: {claude_binary}") from e

    if cp.returncode != 0:
        raise MaxCallError(f"Claude CLI exited {cp.returncode}: {cp.stderr.strip()[:500]}")

    try:
        outer = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise MaxCallError(f"Claude CLI stdout is not JSON: {e}") from e

    raw_text = outer.get("result", "")
    if not isinstance(raw_text, str):
        raise MaxCallError("Claude CLI returned non-string result field")

    parsed: Any | None
    try:
        parsed = json.loads(_strip_markdown_fences(raw_text))
    except json.JSONDecodeError:
        parsed = None

    duration = time.monotonic() - t0
    usage = outer.get("usage") or {}
    return MaxCallResult(
        raw_text=raw_text,
        parsed=parsed,
        usage=usage,
        total_cost_usd=outer.get("total_cost_usd"),
        duration_s=duration,
        tokens={
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
        },
    )
