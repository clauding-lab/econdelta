from datetime import date

from media_screen.digest import format_digest
from media_screen.types import Candidate


def _cand():
    return Candidate("gross_npl_ratio", 35.73, date(2025, 9, 30), 32.26, date(2026, 3, 31),
                     "fresher_period", "tbsnews", "http://x", "NPLs 32.26% end-March 2026", "c")


def test_digest_empty_returns_none():
    assert format_digest([]) is None


def test_digest_lists_each_candidate():
    title, message, fields = format_digest([_cand()])
    assert "1" in title
    assert "gross_npl_ratio" in message and "32.26" in message and "2026-03-31" in message
    assert "reply" in message.lower()  # tells Adnan how to act (approve N / reject N)
