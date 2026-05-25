"""Generate docs/indicator-catalog.md from the authoritative sources.

Sources walked, in order of precedence:
  1. config/sources-v3.json — every indicator EconDelta scrapes.
  2. aggregate_latest.BRIEF_ALIASES — brief-side metric_ids that mirror
     a scraped indicator.
  3. aggregate_latest.BRIEF_CONVERSIONS — brief-side metric_ids that
     are unit conversions of a scraped indicator (e.g. T-Bill outstanding
     mn → crore).
  4. Cross-source aggregate keys (NBR cross-check, etc.) added by
     `_apply_brief_aliases` — listed manually below.

Output is a markdown file with a single sortable table; consumers
(human or LLM) skim it to find the right metric_id for whatever
section they're building.

Re-run after adding indicators or aliases:

    python3 scripts/build_catalog.py > docs/indicator-catalog.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_V3 = REPO_ROOT / "config" / "sources-v3.json"


def _load_sources_v3() -> list[dict]:
    return json.loads(SOURCES_V3.read_text())["indicators"]


def _load_aliases_and_conversions() -> tuple[dict[str, str], dict[str, tuple[str, float]]]:
    # Import lazily so script can run without pip-installed deps.
    sys.path.insert(0, str(REPO_ROOT))
    from aggregate_latest import BRIEF_ALIASES, BRIEF_CONVERSIONS
    return dict(BRIEF_ALIASES), dict(BRIEF_CONVERSIONS)


# Manually-curated keys that are derived inside ``_apply_brief_aliases``
# rather than via a simple alias / conversion. Listed so they appear in
# the catalog with a description.
DERIVED_KEYS: list[tuple[str, str, str, str]] = [
    # (metric_id, unit, cadence, description)
    (
        "nbr_fytd_collected_cr",
        "amount_bdt_crore",
        "monthly",
        "NBR fiscal-year-to-date collection — sourced canonically from "
        "tax_revenue (BB PDF, deterministic parse, 5% anomaly threshold). "
        "News corroborators (TBS, Daily Star) retired 2026-05-25.",
    ),
    (
        "nbr_fytd_cross_check",
        "string",
        "monthly",
        "Cross-check status for nbr_fytd_collected_cr — now always "
        "'single_source_tax_revenue' since the news corroborator path was "
        "retired 2026-05-25. Strings only land in latest.json — NOT in "
        "metric_history (writer filters strings).",
    ),
]


def _format_unit(value_type: str) -> str:
    return f"`{value_type}`"


def _format_range(valid_range: list) -> str:
    if not valid_range or len(valid_range) != 2:
        return "—"
    lo, hi = valid_range
    return f"[{lo}, {hi}]"


def main() -> int:
    indicators = _load_sources_v3()
    aliases, conversions = _load_aliases_and_conversions()

    # Index source indicators by id for cross-reference.
    by_id = {ind["id"]: ind for ind in indicators}

    # Build rows for the table. Each row: (section, metric_id, unit, cadence,
    # source_url_short, range, description).
    rows: list[tuple[str, str, str, str, str, str, str]] = []

    # 1. Direct EconDelta indicators.
    for ind in indicators:
        rows.append((
            ind.get("domain", "—"),
            ind["id"],
            _format_unit(ind["parse"]["value_type"]),
            ind.get("cadence", "—"),
            _short_source(ind["fetch"]["url"]),
            _format_range(ind["parse"].get("valid_range", [])),
            ind.get("name", ""),
        ))

    # 2. Brief-side aliases (1:1 with EconDelta source).
    for brief_id, src_id in aliases.items():
        src = by_id.get(src_id)
        if src is None:
            continue
        rows.append((
            src.get("domain", "—") + " (brief alias)",
            brief_id,
            _format_unit(src["parse"]["value_type"]),
            src.get("cadence", "—"),
            _short_source(src["fetch"]["url"]),
            _format_range(src["parse"].get("valid_range", [])),
            f"Alias of `{src_id}` — {src.get('name', '')}",
        ))

    # 3. Unit conversions.
    for brief_id, (src_id, mult) in conversions.items():
        src = by_id.get(src_id)
        if src is None:
            continue
        # Conversion changes the unit. We special-case the known ones:
        new_unit = (
            "amount_bdt_crore" if mult == 0.1 and src["parse"]["value_type"] == "amount_bdt_mn"
            else src["parse"]["value_type"]
        )
        rows.append((
            src.get("domain", "—") + " (brief conversion)",
            brief_id,
            _format_unit(new_unit),
            src.get("cadence", "—"),
            _short_source(src["fetch"]["url"]),
            "—",
            f"Conversion of `{src_id}` × {mult} — {src.get('name', '')}",
        ))

    # 4. Derived keys.
    for metric_id, unit, cadence, desc in DERIVED_KEYS:
        rows.append((
            "derived (cross-source)",
            metric_id,
            _format_unit(unit),
            cadence,
            "—",
            "—",
            desc,
        ))

    rows.sort(key=lambda r: (r[0], r[1]))

    out: list[str] = []
    out.append("# Indicator catalog\n")
    out.append("**Generated** by `scripts/build_catalog.py` from "
               "`config/sources-v3.json` + `aggregate_latest.BRIEF_ALIASES` "
               "+ `aggregate_latest.BRIEF_CONVERSIONS` plus a manually-curated "
               "list of derived/cross-source keys. Re-run after adding "
               "indicators:\n")
    out.append("```bash\npython3 scripts/build_catalog.py > docs/indicator-catalog.md\n```\n")
    out.append(f"**{len(indicators)}** scraped indicators × **{len(aliases)}** "
               f"brief aliases × **{len(conversions)}** unit conversions × "
               f"**{len(DERIVED_KEYS)}** derived = **{len(rows)}** total entries.\n")
    out.append("Read the data contract for column semantics and query "
               "examples: [`data-contract.md`](data-contract.md).\n")
    out.append("---\n")
    out.append("| Section | metric_id | Unit | Cadence | Source | Valid range | Description |")
    out.append("|---------|-----------|------|---------|--------|-------------|-------------|")
    for section, metric_id, unit, cadence, source_short, range_str, desc in rows:
        # Escape pipe characters in description so the markdown table doesn't break.
        clean_desc = desc.replace("|", "\\|")
        out.append(
            f"| {section} | `{metric_id}` | {unit} | {cadence} | "
            f"{source_short} | {range_str} | {clean_desc} |"
        )

    out.append("")
    print("\n".join(out))
    return 0


def _short_source(url: str) -> str:
    """Compact representation of a fetch URL for the catalog table."""
    if "bb.org.bd" in url:
        return "BB"
    if "bbs.gov.bd" in url:
        return "BBS"
    if "tbsnews.net" in url:
        return "TBS"
    if "thedailystar.net" in url:
        return "Daily Star"
    if "dam.gov.bd" in url:
        return "DAM"
    if "dsebd.org" in url:
        return "DSE"
    if "gsom.bb.org.bd" in url:
        return "BB GSOM"
    return url.split("/")[2] if "://" in url else url


if __name__ == "__main__":
    sys.exit(main())
