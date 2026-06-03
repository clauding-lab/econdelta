from unittest.mock import MagicMock

import requests

from utils.supabase_reader import get_active_media_review
from utils.supabase_writer import set_media_review_status


def _read_session(rows):
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = rows
    sess.get.return_value = resp
    return sess


def test_get_active_filters_to_approved_and_applied():
    sess = _read_session([{"id": 1, "metric_id": "gross_npl_ratio", "status": "approved"}])
    out = get_active_media_review(url="https://x.supabase.co", key="sk", session=sess)
    assert out and out[0]["metric_id"] == "gross_npl_ratio"
    called = sess.get.call_args[0][0]
    assert "status=in.(approved,applied)" in called


def test_set_status_patches_row():
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = 204
    resp.text = ""
    sess.patch.return_value = resp
    set_media_review_status(7, "applied", applied=True,
                            url="https://x.supabase.co", service_key="sk", session=sess)
    url = sess.patch.call_args[0][0]
    body = sess.patch.call_args[1]["json"]
    assert "id=eq.7" in url and body["status"] == "applied" and "applied_at" in body


def test_set_status_without_applied_omits_timestamp():
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = 204
    resp.text = ""
    sess.patch.return_value = resp
    set_media_review_status(3, "superseded", url="https://x.supabase.co",
                            service_key="sk", session=sess)
    assert sess.patch.call_args[1]["json"] == {"status": "superseded"}
