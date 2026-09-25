"""Pure observation transformations: no source reads, writes, or clock lookups.

Adapters supply records in preference order and classify vintage using the existing
sentinel policy. E2 can replace a base with a dated held record, then call
expand_aliases to rebuild its family. Never recover evidence by value equality.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Iterable, Literal, Mapping


@dataclass(frozen=True)
class Observation:
    metric_id: str
    value: float | None
    as_of: date | None
    unit: str
    source: str
    source_url: str | None
    captured_at: datetime
    quality: Literal["verified", "held", "unavailable"]
    date_basis: Literal["observation", "writer_confirmation", "unknown"]
    evidence: str
    dependencies: tuple[str, ...] = ()
    release_status: Literal["final", "provisional", "unknown"] = "unknown"


WRITER_CONFIRMATION_IDS = frozenset({"policy_rate_repo", "policy_rate_sdf", "policy_rate_slf"})
MAX_QUARANTINE_FIELDS = 5


def finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def eligible(observation: Observation, *, today: date) -> bool:
    return (
        finite_number(observation.value)
        and observation.as_of is not None
        and observation.as_of <= today
        and observation.quality in {"verified", "held"}
        and (
            observation.date_basis == "observation"
            or (
                observation.date_basis == "writer_confirmation"
                and observation.metric_id in WRITER_CONFIRMATION_IDS
            )
        )
        and observation.captured_at.utcoffset() is not None
    )


def select_observation(candidates: Iterable[Observation], *, today: date) -> Observation | None:
    """Prefer the first current source; otherwise keep the newest dated held record.

    Adapters mark stale records held using observation vintage, independently of
    download time. A held first candidate therefore cannot shadow fresh evidence.
    """
    valid = [item for item in candidates if eligible(item, today=today)]
    return next((item for item in valid if item.quality == "verified"), None) or max(
        valid, key=lambda item: item.as_of, default=None
    )


def parse_date(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


_UNITS = {
    "percent": "%",
    "amount_bdt_crore": "BDT crore",
    "amount_bdt_mn": "BDT million",
    "amount_usd_bn": "USD billion",
    "amount_usd_mn": "USD million",
    "amount_bdt_billion": "BDT billion",
    "ratio": "ratio",
    "index": "index",
}


def from_snapshot(metric_id: str, snapshot: Mapping, *, captured_at: datetime) -> Observation:
    """Adapt explicit source metadata; an old scalar is never verified by default."""
    raw_capture = snapshot.get("scraped_at")
    try:
        capture = datetime.fromisoformat(str(raw_capture).replace("Z", "+00:00"))
        if capture.utcoffset() is None:
            capture = captured_at
    except (ValueError, TypeError):
        capture = captured_at
    as_of = parse_date(snapshot.get("source_as_of"))
    basis = "observation" if as_of else "unknown"
    provenance = snapshot.get("_provenance")
    if metric_id in WRITER_CONFIRMATION_IDS and provenance in {
        "deterministic",
        "llm_extracted",
        "llm_corrected",
        "stale_fallback",
    }:
        # This confirms the standing panel reading, never its decision date.
        as_of = parse_date(str(raw_capture)[:10])
        basis = "writer_confirmation" if as_of else "unknown"
    numeric = finite_number(snapshot.get("value"))
    trusted = provenance in {"deterministic", "llm_extracted", "llm_corrected", "stale_fallback"}
    quality = "held" if provenance == "stale_fallback" else "verified"
    if not numeric or not trusted or as_of is None or as_of > captured_at.date():
        quality = "unavailable"
    return Observation(
        metric_id,
        snapshot.get("value") if numeric else None,
        as_of,
        snapshot.get("unit") or _UNITS.get(snapshot.get("value_type"), "unknown"),
        snapshot.get("source") or metric_id,
        snapshot.get("source_url"),
        capture,
        quality,
        basis,
        str(
            snapshot.get("evidence")
            or " | ".join(
                str(snapshot[k])
                for k in ("_artifact_sha256", "_parse_strategy", "source_as_of", "sanity_note")
                if snapshot.get(k)
            )
            or "No recoverable source evidence"
        ),
        release_status=snapshot.get("release_status", "unknown")
        if snapshot.get("release_status", "unknown") in {"final", "provisional", "unknown"}
        else "unknown",
    )


def serialize_observations(observations: Mapping[str, Observation]) -> dict[str, dict]:
    result = {}
    for mid, obs in observations.items():
        item = asdict(obs)
        item["as_of"] = obs.as_of.isoformat() if obs.as_of else None
        item["captured_at"] = obs.captured_at.isoformat()
        item["dependencies"] = list(obs.dependencies)
        result[mid] = item
    return result


def derive_ratio(
    metric_id: str, numerator: Observation, denominator: Observation
) -> Observation | None:
    if (
        numerator.as_of is None
        or numerator.as_of != denominator.as_of
        or not finite_number(numerator.value)
        or not finite_number(denominator.value)
        or denominator.value <= 0
        or numerator.unit != denominator.unit
        or numerator.unit == "unknown"
        or numerator.date_basis != "observation"
        or denominator.date_basis != "observation"
        or numerator.quality == "unavailable"
        or denominator.quality == "unavailable"
    ):
        return None
    value = round(numerator.value / denominator.value * 100, 4)
    if not finite_number(value):
        return None
    return replace(
        numerator,
        metric_id=metric_id,
        value=value,
        unit="%",
        quality="held" if "held" in (numerator.quality, denominator.quality) else "verified",
        captured_at=max(numerator.captured_at, denominator.captured_at),
        dependencies=(numerator.metric_id, denominator.metric_id),
        source=numerator.source
        if numerator.source == denominator.source
        else f"{numerator.source}; {denominator.source}",
        source_url=numerator.source_url if numerator.source_url == denominator.source_url else None,
        evidence=f"{numerator.metric_id}: {numerator.evidence}; {denominator.metric_id}: {denominator.evidence}",
        release_status="provisional"
        if "provisional" in (numerator.release_status, denominator.release_status)
        else "final"
        if numerator.release_status == denominator.release_status == "final"
        else "unknown",
    )


BRIEF_ALIASES: dict[str, str] = {
    # macro
    "macro_cpi_food": "food_inflation",
    "macro_cpi_headline": "general_inflation",
    "macro_cpi_nonfood": "non_food_inflation",
    # YoY % credit growth — repointed PR-C (build-brief item 4) to BB's live
    # econdata/monetarysurvey HTML page ("Claims on Private Sector (DMBs)"),
    # not derived from the absolute private_sector_credit BDT-crore value.
    #
    # OWNER DECISION FLAG (2026-08-22, PR-C): June 2026 has a genuine
    # conflict between BB's own machine-readable table and unanimous press
    # coverage of the same concept. BB's econdata/monetarysurvey table
    # ("Claims on Private Sector (DMBs)" YoY column) reads 4.53%; every
    # press outlet quoted BB's own ADJUSTED headline figure of 4.47% for
    # the same month. This PR ships 4.53% (the BB table -- machine-
    # readable, matches the series' own prior-month trajectory: Mar 4.72,
    # Apr 4.75, May 4.98) as the live value, per the source scout's
    # recommendation. Do NOT average the two, and do NOT silently swap to
    # 4.47% without a fresh sign-off -- this is a data-source judgment
    # call on a number The Brief publishes as "private credit growth", not
    # an engineering decision. See AGENT_LEARNINGS.md/AGENTS.md landmine 52
    # for the fuller writeup.
    "macro_credit_growth": "private_sector_credit_yoy_pct",
    # remittance — bn→mn unit conversion is in BRIEF_CONVERSIONS below.
    # fiscal — crore→trillion conversions are in BRIEF_CONVERSIONS below.
    # NBR FYTD canonical: tax_revenue from the BB PDF (deterministic parse,
    # 5% anomaly threshold). News corroborators (nbr_fytd_collected_tbs,
    # nbr_fytd_collected_dailystar) retired 2026-05-25 — both tag-listing
    # pages drifted onto articles covering different fiscal-year windows,
    # so the cross-check flapped.
    "nbr_fytd_collected_cr": "tax_revenue",
    # banking primitives
    "banking_broad_money": "broad_money",
    "banking_reserve_money": "reserve_money",
    "banking_money_multiplier": "money_multiplier",
    "banking_excess_liquid": "excess_liquid_asset_total_minimum",
    "banking_deposits": "deposits_of_the_system",
    "banking_call_money_rate": "call_money_rate",
    # banking ratios (FSAR — quarterly)
    "banking_npl_pct": "gross_npl_ratio",
    "banking_car_pct": "banking_sector_crar",
    # money market — yield headline (daily)
    "tbill_91d_yield_pct": "bill_bond_rates",
    "gsec_next_auction_cr": "gsec_auction",
    # money market — brief metric_id forms (the brief's tbond builder
    # uses ``tbond_tbill_91d``; brief's nbr/dam builders use ``dam_*``)
    "tbond_tbill_91d": "bill_bond_rates",
    # multi-tenor T-Bill / T-Bond yields — feed §07 yield curve chart
    "tbond_tbill_182d": "tbill_182d_yield",
    "tbond_tbill_364d": "tbill_364d_yield",
    "tbond_bond_5y": "tbond_5y_yield",
    "tbond_bond_10y": "tbond_10y_yield",
    # DAM retail food prices (daily, BDT/kg or BDT/4-pcs for eggs)
    "food_rice_coarse_bdt": "food_rice_coarse",
    "food_atta_packet_bdt": "food_atta_packet",
    "food_egg_red_bdt": "food_egg_red",
    "food_chicken_farm_bdt": "food_chicken_farm",
    "food_oil_soybean_bdt": "food_oil_soybean",
    "food_onion_local_bdt": "food_onion_local",
    "food_lentil_moong_bdt": "food_lentil_moong",
    "food_sugar_local_bdt": "food_sugar_local",
    # DAM retail food prices — brief metric_id forms (`dam_*`)
    "dam_rice_coarse": "food_rice_coarse",
    "dam_lentil": "food_lentil_moong",
    "dam_oil": "food_oil_soybean",
    "dam_sugar": "food_sugar_local",
    "dam_onion": "food_onion_local",
    "dam_egg": "food_egg_red",
    "dam_chicken": "food_chicken_farm",
    "dam_flour": "food_atta_packet",
}

# Aliases that need a unit conversion (source unit → brief unit).
# Format: brief_key → (source_key, multiplier).
BRIEF_CONVERSIONS: dict[str, tuple[str, float]] = {
    # T-Bill / T-Bond outstanding: gsom reports BDT million; brief expects
    # BDT crore (1 crore = 10 million → multiplier 0.1).
    "tbill_outstanding_cr": ("treasury_bill_outstanding", 0.1),
    "tbond_outstanding_cr": ("treasury_bond_outstanding", 0.1),
    # Fiscal: EconDelta indicators are BDT crore, brief renders BDT trillion.
    # 1 trillion BDT = 100,000 crore → multiplier 0.00001.
    "fiscal_nbr_collected_trn": ("tax_revenue", 0.00001),
    "fiscal_govt_borrow_trn": ("domestic_borrowing_for_budget_deficit", 0.00001),
    "fiscal_foreign_borrow_trn": ("foreign_borrowing_for_budget_deficit", 0.00001),
    "fiscal_bank_borrow_trn": ("bank_borrowing_for_deficit_financing", 0.00001),
    "fiscal_nsc_outstanding": ("nsc_outstanding", 0.00001),
    # Remittance: EconDelta source is USD billion, brief renders USD million.
    # 1 billion = 1,000 million → multiplier 1000.
    "remit_monthly_mn": ("monthly_remittance", 1000.0),
    "remit_fy_mn": ("fy_remittance", 1000.0),
    # NBR component decomposition (Phase 3.2): articles report BDT crore,
    # brief's §12 expects BDT bn. 1 bn = 100 crore → multiplier 0.01.
    "nbr_vat_bn": ("nbr_vat_collected_cr", 0.01),
    "nbr_it_bn": ("nbr_it_collected_cr", 0.01),
    "nbr_customs_bn": ("nbr_customs_collected_cr", 0.01),
}

RESERVE_UTIL_DERIVED: dict[str, tuple[str, str]] = {
    # derived_id -> (numerator_id, denominator_id)
    "crr_utilisation_pct": ("deposits_held_with_bb_crr", "deposits_of_the_system"),
    "slr_utilisation_pct": ("excess_liquid_asset_total_minimum", "deposits_of_the_system"),
}

_CONVERSION_UNITS = {
    "tbill_outstanding_cr": "BDT crore",
    "tbond_outstanding_cr": "BDT crore",
    "remit_monthly_mn": "USD million",
    "remit_fy_mn": "USD million",
    "nbr_vat_bn": "BDT billion",
    "nbr_it_bn": "BDT billion",
    "nbr_customs_bn": "BDT billion",
    **{mid: "BDT trillion" for mid in BRIEF_CONVERSIONS if mid.startswith("fiscal_")},
}


def expand_aliases(observations: Mapping[str, Observation]) -> dict[str, Observation]:
    """Rebuild aliases and ratios, replacing old children rather than retaining them."""
    children = set(BRIEF_ALIASES) | set(BRIEF_CONVERSIONS) | set(RESERVE_UTIL_DERIVED)
    result = {mid: obs for mid, obs in observations.items() if mid not in children}
    for child, parent in BRIEF_ALIASES.items():
        if parent in result:
            result[child] = replace(result[parent], metric_id=child, dependencies=(parent,))
    for child, (parent, multiplier) in BRIEF_CONVERSIONS.items():
        if parent in result:
            base = result[parent]
            value = round(base.value * multiplier, 2) if finite_number(base.value) else None
            result[child] = replace(
                base,
                metric_id=child,
                value=value if finite_number(value) else None,
                quality=base.quality if finite_number(value) else "unavailable",
                unit=_CONVERSION_UNITS[child],
                dependencies=(parent,),
            )
    for mid, (num, den) in RESERVE_UTIL_DERIVED.items():
        if num in result and den in result:
            derived = derive_ratio(mid, result[num], result[den])
            if derived is not None:
                result[mid] = derived
    return result


def _history_observation(value: object, metric_id: str) -> Observation | None:
    """Decode one archived observation without inventing a period or evidence."""
    if isinstance(value, Observation):
        obs = value
    elif isinstance(value, Mapping):
        as_of = parse_date(value.get("as_of"))
        try:
            captured_at = datetime.fromisoformat(
                str(value.get("captured_at", "")).replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if captured_at.utcoffset() is None:
            return None
        quality = value.get("quality")
        basis = value.get("date_basis")
        if quality not in {"verified", "held"} or basis not in {
            "observation",
            "writer_confirmation",
        }:
            return None
        deps = value.get("dependencies", ())
        if not isinstance(deps, (list, tuple)) or not all(isinstance(d, str) for d in deps):
            return None
        obs = Observation(
            metric_id=str(value.get("metric_id") or metric_id),
            value=value.get("value"),
            as_of=as_of,
            unit=str(value.get("unit") or "unknown"),
            source=str(value.get("source") or "archive"),
            source_url=value.get("source_url")
            if isinstance(value.get("source_url"), str)
            else None,
            captured_at=captured_at,
            quality=quality,
            date_basis=basis,
            evidence=str(value.get("evidence") or ""),
            dependencies=tuple(deps),
            release_status=value.get("release_status", "unknown")
            if value.get("release_status", "unknown") in {"final", "provisional", "unknown"}
            else "unknown",
        )
    else:
        return None
    if (
        obs.metric_id != metric_id
        or not finite_number(obs.value)
        or obs.as_of is None
        or obs.as_of > obs.captured_at.date()
        or obs.quality not in {"verified", "held"}
        or obs.date_basis not in {"observation", "writer_confirmation"}
        or (
            obs.date_basis == "writer_confirmation" and obs.metric_id not in WRITER_CONFIRMATION_IDS
        )
        or obs.captured_at.utcoffset() is None
        or not obs.evidence.strip()
    ):
        return None
    return obs


def quarantine_observations(
    current: Mapping[str, Observation],
    flagged_ids: list[str],
    history: list[dict],
    *,
    breadth_count: int,
) -> tuple[dict[str, Observation], list[str], bool]:
    """Replace rejected dependency families only with dated archived evidence.

    Aliases and ratios are rebuilt from held source observations. If any needed
    predecessor has no dated archive record, its whole dependent family is
    omitted. Unknown ids and broad verdicts remain hard rejects.
    """
    flagged = sorted(set(flagged_ids))
    if any(not isinstance(mid, str) or mid not in current for mid in flagged):
        return dict(current), [], True
    if breadth_count > MAX_QUARANTINE_FIELDS:
        return dict(current), [], True
    if not flagged:
        return dict(current), [], False

    def roots_for(mid: str, seen: set[str] | None = None) -> set[str]:
        seen = set() if seen is None else seen
        if mid in seen:
            return {mid}
        seen.add(mid)
        obs = current.get(mid)
        if obs is None or not obs.dependencies:
            return {mid}
        roots: set[str] = set()
        for dependency in obs.dependencies:
            roots.update(roots_for(dependency, seen))
        return roots

    roots = set().union(*(roots_for(mid) for mid in flagged))
    # Current capture time bounds any recovered source period; archive ordering
    # or a future-dated archive must never turn into a future observation.
    current_dates = [
        obs.captured_at.date()
        for obs in current.values()
        if obs.captured_at.utcoffset() is not None
    ]
    today = max(current_dates, default=date.min)

    replacements: dict[str, Observation] = {}
    for root in roots:
        candidates = []
        for archived in history:
            records = archived.get("observations") if isinstance(archived, Mapping) else None
            if isinstance(records, Mapping):
                obs = _history_observation(records.get(root), root)
                if obs is not None and obs.as_of <= today:
                    candidates.append(obs)
                continue
            # Compatibility with a historical caller that already carries
            # explicit per-id dates. Undated scalar archives are never trusted.
            data = archived.get("data", {}) if isinstance(archived, Mapping) else {}
            dates = archived.get("source_as_of", {}) if isinstance(archived, Mapping) else {}
            if not isinstance(data, Mapping) or not isinstance(dates, Mapping):
                continue
            as_of = parse_date(dates.get(root))
            scalar = data.get(root)
            if as_of is None or as_of > today or not finite_number(scalar):
                continue
            capture_raw = archived.get("updated_at")
            try:
                captured = datetime.fromisoformat(str(capture_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if captured.utcoffset() is None:
                continue
            candidates.append(
                Observation(
                    metric_id=root,
                    value=scalar,
                    as_of=as_of,
                    unit="unknown",
                    source="archive",
                    source_url=None,
                    captured_at=captured,
                    quality="verified",
                    date_basis="observation",
                    evidence=f"Recovered from dated archive record for {root} ({as_of})",
                )
            )
        if candidates:
            predecessor = max(candidates, key=lambda obs: (obs.as_of, obs.captured_at))
            replacements[root] = replace(
                predecessor,
                quality="held",
                evidence=f"{predecessor.evidence}; held after review rejection",
            )

    family_ids = {mid for mid in current if roots_for(mid) & roots}
    retained = {mid: obs for mid, obs in current.items() if mid not in family_ids}
    retained.update(replacements)
    accepted = expand_aliases(retained)
    return accepted, flagged, False
