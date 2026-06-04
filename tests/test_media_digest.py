from datetime import date

from media_screen.digest import format_report
from media_screen.types import Candidate, Skip


def _cand():
    return Candidate("gross_npl_ratio", 35.73, date(2025, 9, 30), 32.26, date(2026, 3, 31),
                     "fresher_period", "tbsnews", "http://x", "NPLs 32.26% end-March 2026", "c")


def test_zero_articles_message():
    title, message, fields = format_report([], [], 0, 0)
    assert "0 articles" in title and "all sources failed" in message.lower()


def test_no_tracked_figures_heartbeat():
    title, message, fields = format_report([], [], 6, 6)
    assert "no change" in title.lower()
    assert "no tracked figures" in message.lower()
    assert "Checked 12 articles (6 TBS, 6 Daily Star)" in message


def test_heartbeat_renders_skip_reason_text():
    skip = Skip("gross_npl_ratio", 32.26, date(2026, 3, 31), "matches-current-data")
    title, message, fields = format_report([], [skip], 6, 6)
    assert "no change" in title.lower()
    assert "matches current data" in message
    assert "gross_npl_ratio" in message and "32.26" in message
    assert "nothing needs approval" in message.lower()


def test_candidate_uses_real_id_and_approve_reject():
    title, message, fields = format_report([(42, _cand())], [], 5, 6)
    assert "1 needs approval" in title
    assert "#42" in message and "32.26" in message
    assert "approve 42" in message and "reject 42" in message
    assert fields == {"gross_npl_ratio": "32.26 @ 2026-03-31"}


def test_dry_run_candidate_has_no_id_or_approve_line():
    title, message, fields = format_report([(None, _cand())], [], 5, 6)
    assert "dry-run" in message.lower()
    assert "approve" not in message.lower()


def test_skip_cap_truncates_with_footer():
    skips = [Skip(f"m{i}", float(i), date(2026, 3, 31), "matches-current-data")
             for i in range(20)]
    title, message, fields = format_report([], skips, 6, 6)
    assert "…and 8 more" in message
