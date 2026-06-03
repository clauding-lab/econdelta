from unittest.mock import MagicMock

import pytest
import requests

from utils.supabase_writer import decide_media_review


def _session(returned_rows, status=200):
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    resp.json.return_value = returned_rows
    sess.patch.return_value = resp
    return sess


def test_approve_patches_pending_row_with_actor():
    sess = _session([{"id": 7}])
    n = decide_media_review(7, "approve", actor="discord:adnan",
                            url="https://x.supabase.co", service_key="sk", session=sess)
    assert n == 1
    url = sess.patch.call_args[0][0]
    body = sess.patch.call_args[1]["json"]
    assert "id=eq.7" in url and "status=eq.pending" in url      # race-safe: only flips pending
    assert body["status"] == "approved" and body["decided_by"] == "discord:adnan"
    assert "decided_at" in body


def test_reject_maps_to_rejected():
    sess = _session([{"id": 3}])
    decide_media_review(3, "reject", actor="cli", url="https://x.supabase.co",
                        service_key="sk", session=sess)
    assert sess.patch.call_args[1]["json"]["status"] == "rejected"


def test_already_decided_row_is_noop_returns_zero():
    sess = _session([])  # PATCH matched nothing (row not pending)
    n = decide_media_review(7, "approve", actor="cli", url="https://x.supabase.co",
                            service_key="sk", session=sess)
    assert n == 0


def test_unknown_decision_raises():
    with pytest.raises(ValueError):
        decide_media_review(7, "maybe", actor="cli", url="https://x.supabase.co",
                            service_key="sk", session=_session([]))
