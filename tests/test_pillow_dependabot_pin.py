"""Guard: Pillow's pin must clear the highest Dependabot-patched version.

Dependabot opened 26 alerts (20 high, 6 medium) against Pillow as of
2026-08-08, all sourced from the SAME stale pin: ``Pillow>=10,<12`` in both
``requirements.txt`` and ``pyproject.toml``. The upper bound (``<12``) is the
actual defect — it caps installs below every GHSA's first-patched version
(12.2.0 for the two decompression-bomb/DoS mediums, 12.3.0 for the 20 highs,
e.g. GHSA-jjj6-mw9f-p565 / CVE-2026-59200, PdfParser decompression-bomb DoS),
so `pip install` never resolves to a fixed release even though the lower
bound looks harmless. Pillow has no direct call sites in this repo (grep
confirms) — it's pulled in transitively via pytesseract/pdf2image for the
OCR fallback in parsers/hybrid.py, so bumping the pin carries no known API
migration for our code.

This test re-derives the requirement from BOTH manifests and fails if either
one's lower bound sits below the known-vulnerable ceiling. It intentionally
does NOT also assert the pin still admits ``MIN_PATCHED_PILLOW`` exactly —
that would fight the next legitimate security bump (e.g. a future
``Pillow>=13.1,<14`` correctly stops admitting 12.3.0, and a bare assertion
of that would fail CI on a strictly-safer pin for no reason). The
lower-bound check below is the whole rule.
"""

from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).parent.parent

# Highest first_patched_version across all 26 open Dependabot alerts
# (gh api repos/clauding-lab/econdelta/dependabot/alerts?state=open, 2026-08-08).
MIN_PATCHED_PILLOW = Version("12.3.0")


def _pillow_requirement_from_requirements_txt() -> Requirement:
    text = (REPO_ROOT / "requirements.txt").read_text()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        if canonicalize_name(req.name) == "pillow":
            return req
    raise AssertionError("Pillow not found in requirements.txt")


def _pillow_requirement_from_pyproject() -> Requirement:
    text = (REPO_ROOT / "pyproject.toml").read_text()
    start = text.index("dependencies = [")
    end = text.index("]", start)
    block = text[start:end]
    for raw_line in block.splitlines():
        line = raw_line.split("#", 1)[0].strip().strip(",").strip('"').strip("'")
        if not line:
            continue
        try:
            req = Requirement(line)
        except Exception:
            continue
        if canonicalize_name(req.name) == "pillow":
            return req
    raise AssertionError("Pillow not found in pyproject.toml dependencies")


def _lower_bound_version(spec_version: str) -> Version:
    """Parse a specifier's version string, tolerating a trailing wildcard.

    A conventional ``Pillow==12.3.*`` pin is a valid, safe lower bound but
    ``Version("12.3.*")`` raises ``InvalidVersion`` — strip the wildcard
    segment first so it reads as the equivalent ``12.3``.
    """
    if spec_version.endswith(".*"):
        spec_version = spec_version[:-2]
    return Version(spec_version)


def _min_version_allowed(req: Requirement) -> Version:
    """Smallest version consistent with every specifier on the requirement."""
    candidates = [
        _lower_bound_version(spec.version)
        for spec in req.specifier
        if spec.operator in (">=", "==", "~=")
    ]
    assert candidates, f"{req} has no lower-bound specifier to check"
    return min(candidates)


def test_requirements_txt_pillow_pin_clears_patched_version():
    req = _pillow_requirement_from_requirements_txt()
    assert _min_version_allowed(req) >= MIN_PATCHED_PILLOW, (
        f"requirements.txt Pillow pin ({req}) allows versions below "
        f"{MIN_PATCHED_PILLOW}, which carries known Dependabot HIGH alerts"
    )


def test_pyproject_toml_pillow_pin_clears_patched_version():
    req = _pillow_requirement_from_pyproject()
    assert _min_version_allowed(req) >= MIN_PATCHED_PILLOW, (
        f"pyproject.toml Pillow pin ({req}) allows versions below "
        f"{MIN_PATCHED_PILLOW}, which carries known Dependabot HIGH alerts"
    )
