"""Prompt construction + output validation for the weekly briefing.

The model is a writer, not a calculator: it receives a Python-built digest and
the pre-computed anomaly candidates, and may only REFERENCE candidate ids — it
cannot invent numbers. validate_output enforces that at the data layer.
"""
from __future__ import annotations

import json
from typing import Any

_VALID_THREAD_STATUS = {"open", "resolved"}

PROMPT_TEMPLATE = """You are the desk economist for IDLC Finance PLC's ALCO. Write the weekly \
Bangladesh money-market briefing for the week of {week_of}. Audience: senior bankers.

You are given (1) a DIGEST of the latest readings + stats, (2) pre-computed ANOMALY CANDIDATES \
(the numbers are authoritative — do not recompute or invent figures), (3) the PRIOR BRIEFINGS for \
continuity, and (4) the current OPEN THREADS you have been tracking.

Return ONLY a JSON object with this exact shape:
{{
  "title": "<a sharp one-line headline>",
  "body": "<GitHub-flavored Markdown — see FORMAT below; reference figures only from the digest/candidates>",
  "featured_anomalies": [{{"candidate_id": "<MUST be one of the provided candidate ids>", "why": "<one line of ALCO relevance>"}}],
  "updated_threads": [{{"id": "<stable slug>", "thread": "<short name>", "status": "open|resolved", "since_week": "<ISO week>", "note": "<follow-through>"}}]
}}

FORMAT the body for sharp, scannable reading by a banker on mobile — NOT one long paragraph:
- Open with a 1-2 sentence lede.
- Then 2-4 `## ` sub-headings (e.g. "What moved", "Drivers", "ALCO implications", "Watch").
- Under each, lead with `- ` bullet points for the key figures and takeaways; keep prose to at most one short connective sentence per section.
- Keep it tight — UNDER ~250 words total. Use `**bold**` sparingly, for the single most important figure.

Carry forward the open threads: mark resolved ones resolved, keep live ones open with an updated note, \
and add new threads for newly material developments. Do not feature an anomaly whose candidate_id is \
not in the provided list.

DIGEST:
{digest_json}

ANOMALY CANDIDATES:
{candidates_json}

PRIOR BRIEFINGS (newest first):
{prior_briefings_json}

OPEN THREADS:
{open_threads_json}
"""


_MAX_PRIOR_CHARS = 120_000


def build_prompt(*, digest: dict, candidates: list[dict], prior_briefings: list[dict],
                 open_threads: list[dict], week_of: str) -> str:
    # Trim WHOLE prior briefings (newest-first) until the block fits, so the model
    # never receives a mid-object-truncated, malformed JSON list.
    prior = list(prior_briefings)
    prior_json = json.dumps(prior, indent=2, default=str)
    while len(prior_json) > _MAX_PRIOR_CHARS and len(prior) > 1:
        prior = prior[:-1]
        prior_json = json.dumps(prior, indent=2, default=str)
    return PROMPT_TEMPLATE.format(
        week_of=week_of,
        digest_json=json.dumps(digest, indent=2, default=str),
        candidates_json=json.dumps(candidates, indent=2, default=str),
        prior_briefings_json=prior_json,
        open_threads_json=json.dumps(open_threads, indent=2, default=str),
    )


class BriefingValidationError(ValueError):
    """Raised when Claude's output is missing, not JSON, or breaks the contract."""


def validate_output(parsed: Any, valid_candidate_ids: set[str]) -> dict:
    if not isinstance(parsed, dict):
        raise BriefingValidationError("output is not JSON (parsed is None or non-object)")
    for field in ("title", "body"):
        if not isinstance(parsed.get(field), str) or not parsed[field].strip():
            raise BriefingValidationError(f"missing or empty required field: {field}")

    feats = parsed.get("featured_anomalies", [])
    if not isinstance(feats, list):
        raise BriefingValidationError("featured_anomalies must be a list")
    for f in feats:
        cid = f.get("candidate_id") if isinstance(f, dict) else None
        if cid not in valid_candidate_ids:
            raise BriefingValidationError(f"unknown candidate_id: {cid!r}")
        why = f.get("why")
        if not isinstance(why, str) or not why.strip():
            raise BriefingValidationError("featured anomaly missing or empty 'why'")

    threads = parsed.get("updated_threads", [])
    if not isinstance(threads, list):
        raise BriefingValidationError("updated_threads must be a list")
    for t in threads:
        if not isinstance(t, dict) or t.get("status") not in _VALID_THREAD_STATUS:
            raise BriefingValidationError(f"thread has bad status: {t}")

    return parsed
