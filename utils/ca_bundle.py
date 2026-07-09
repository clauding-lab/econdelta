"""Combined CA bundle: certifi roots + repo-bundled extra intermediates.

Some servers present an INCOMPLETE TLS chain — they send only the leaf and omit
the issuing intermediate. Browsers AIA-chase the missing intermediate; python
``requests`` / ``urllib3`` do NOT, so verification fails with
``CERTIFICATE_VERIFY_FAILED`` even though the root IS trusted.

Concretely (E1.2, 2026-07-09): DSE renewed its cert ~2026-06-09 and its servers
(``www.dse.com.bd`` and ``dsebd.org``) send the leaf only, omitting
``Sectigo Public Server Authentication CA DV R36``. certifi has the *root*
(``…Root R46``) but not that intermediate, so every DSE fetch broke — the daily
index scraper AND the day-end backfill — for weeks.

The fix is NOT ``verify=False`` (never do that). Instead we merge certifi's
trusted roots with the intermediates vendored under ``fetchers/ca/`` — the ONE
canonical location for vendored intermediates in this repo, shared with the
host-scoped urllib path in ``fetchers/tls.py`` (mof.gov.bd) so there is a single
cert file and a single rotation point (see that module's docstring). The result
is one CA bundle that :class:`utils.http_client.HttpClient` points ``verify`` at.

This is purely ADDITIVE — it adds a publicly-issued intermediate as a chain-
building link, never removes a certifi root — so it cannot weaken verification
for any host: a leaf must still chain to a certifi-trusted ROOT and match the
hostname; an intermediate can't make a bad cert good. Any bundle-build failure
degrades gracefully to plain certifi (no fetch is ever left un-verified).

This travels with the repo, so a from-scratch redeploy verifies DSE without the
manual box-level ``certifi/cacert.pem`` append.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import certifi

logger = logging.getLogger("ca_bundle")

# The single canonical home for vendored intermediates — shared with the
# host-scoped urllib path (fetchers/tls.py). Do NOT create a second cert dir:
# two copies of the same intermediate means two rotation points and a silent
# drift trap when the CA eventually rotates.
_CERTS_DIR = Path(__file__).resolve().parent.parent / "fetchers" / "ca"
_cached_bundle: str | None = None


def combined_ca_bundle() -> str:
    """Return a path to a CA bundle = certifi roots + every ``fetchers/ca/*.pem``.

    Memoised per process (the merged file is written once to the temp dir). On any
    failure — no ``fetchers/ca/`` dir, unreadable PEM, un-writable temp — this
    returns ``certifi.where()`` so callers still verify against the standard root
    store. The bundled intermediate is only additive trust, so losing it never
    weakens verification; it just means an incomplete-chain host (DSE) fails as
    before.
    """
    global _cached_bundle
    if _cached_bundle is not None:
        return _cached_bundle

    base = certifi.where()
    try:
        extras = sorted(_CERTS_DIR.glob("*.pem")) if _CERTS_DIR.is_dir() else []
        if not extras:
            _cached_bundle = base
            return _cached_bundle

        parts = [Path(base).read_text()]
        for pem in extras:
            parts.append(f"\n# --- extra CA bundled from fetchers/ca/{pem.name} ---\n")
            parts.append(pem.read_text())

        fd, path = tempfile.mkstemp(prefix="econdelta-ca-", suffix=".pem")
        with os.fdopen(fd, "w") as fh:
            fh.write("".join(parts))
        _cached_bundle = path
        logger.info(
            "built combined CA bundle (certifi + %d extra cert file(s)) at %s",
            len(extras), path,
        )
    except Exception as e:  # noqa: BLE001 — never let CA-bundle assembly break a fetch
        logger.warning(
            "combined CA bundle build failed (%s); falling back to certifi-only", e,
        )
        _cached_bundle = base

    return _cached_bundle
