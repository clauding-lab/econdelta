"""Tests for utils.ipv4.force_ipv4_only — the scoped IPv4-only resolution guard.

The ExonVPS Dhaka box has IPv6 blackholed for some hosts (thedocs.worldbank.org,
www.imf.org): a dual-stack fetch stalls on the dead AAAA address until timeout.
``force_ipv4_only`` pins urllib3's process-global ``HAS_IPV6`` to False for the
duration of a fetch and MUST restore it afterwards so it cannot bleed into a
later call (e.g. the Supabase upsert in the same one-shot scraper process).
"""

from __future__ import annotations

import pytest
import urllib3.util.connection as u3conn

from utils.ipv4 import force_ipv4_only


@pytest.fixture(autouse=True)
def _restore_has_ipv6():
    """Snapshot/restore the process-global so no test leaks state to the suite."""
    original = u3conn.HAS_IPV6
    yield
    u3conn.HAS_IPV6 = original


def test_forces_ipv4_inside_block():
    u3conn.HAS_IPV6 = True
    with force_ipv4_only():
        assert u3conn.HAS_IPV6 is False


def test_restores_previous_value_after_block():
    u3conn.HAS_IPV6 = True
    with force_ipv4_only():
        pass
    assert u3conn.HAS_IPV6 is True


def test_restores_even_when_block_raises():
    """The bleed bug: an exception inside must not leave HAS_IPV6 stuck at False."""
    u3conn.HAS_IPV6 = True
    with pytest.raises(RuntimeError):
        with force_ipv4_only():
            raise RuntimeError("boom")
    assert u3conn.HAS_IPV6 is True


def test_restores_prior_false_not_hardcoded_true():
    """Restores whatever the prior value was — not a hardcoded True."""
    u3conn.HAS_IPV6 = False
    with force_ipv4_only():
        assert u3conn.HAS_IPV6 is False
    assert u3conn.HAS_IPV6 is False
