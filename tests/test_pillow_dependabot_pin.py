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
one's lower bound sits below the known-vulnerable ceiling.
"""

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).parent.parent

# Highest first_patched_version across all 26 open Dependabot alerts
# (gh api repos/clauding-lab/econdelta/dependabot/alerts?state=open, 2026-08-08).
MIN_PATCHED_PILLOW = Version("12.3.0")


def _pillow_requirement_from_requirements_txt() -> Requirement:
    text = (REPO_ROOT / "requirements.txt").read_text()
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("pillow"):
            return Requirement(line)
    raise AssertionError("Pillow not found in requirements.txt")


def _pillow_requirement_from_pyproject() -> Requirement:
    text = (REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r'"(Pillow[^"]*)"', text)
    assert match, "Pillow not found in pyproject.toml dependencies"
    return Requirement(match.group(1))


def _min_version_allowed(req: Requirement) -> Version:
    """Smallest version consistent with every specifier on the requirement."""
    candidates = [Version(spec.version) for spec in req.specifier if spec.operator in (">=", "==", "~=")]
    assert candidates, f"{req} has no lower-bound specifier to check"
    return min(candidates)


def test_requirements_txt_pillow_pin_clears_patched_version():
    req = _pillow_requirement_from_requirements_txt()
    assert _min_version_allowed(req) >= MIN_PATCHED_PILLOW, (
        f"requirements.txt Pillow pin ({req}) allows versions below "
        f"{MIN_PATCHED_PILLOW}, which carries known Dependabot HIGH alerts"
    )
    # The pin must also not exclude the patched version outright.
    assert req.specifier.contains(str(MIN_PATCHED_PILLOW), prereleases=True)


def test_pyproject_toml_pillow_pin_clears_patched_version():
    req = _pillow_requirement_from_pyproject()
    assert _min_version_allowed(req) >= MIN_PATCHED_PILLOW, (
        f"pyproject.toml Pillow pin ({req}) allows versions below "
        f"{MIN_PATCHED_PILLOW}, which carries known Dependabot HIGH alerts"
    )
    assert req.specifier.contains(str(MIN_PATCHED_PILLOW), prereleases=True)
