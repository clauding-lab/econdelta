from datetime import date
from unittest.mock import MagicMock

import requests

from media_screen.dedup import drop_already_open
from media_screen.types import Candidate
from utils.supabase_writer import insert_media_review_rows


def _cand(metric="gross_npl_ratio", as_of=date(2026, 3, 31)):
    return Candidate(metric, 35.73, date(2025, 9, 30), 32.26, as_of,
                     "fresher_period", "tbs", "http://x", "q", "c")


def test_dedup_drops_candidate_matching_open_row():
    open_rows = [{"metric_id": "gross_npl_ratio", "press_as_of": "2026-03-31", "status": "pending"}]
    assert drop_already_open([_cand()], open_rows) == []


def test_dedup_keeps_new_candidate():
    open_rows = [{"metric_id": "gross_npl_ratio", "press_as_of": "2025-12-31", "status": "pending"}]
    assert len(drop_already_open([_cand()], open_rows)) == 1


def test_insert_returns_inserted_ids():
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = 201
    resp.text = ""
    resp.json.return_value = [{"id": 42}]
    sess.post.return_value = resp
    ids = insert_media_review_rows([_cand()], url="https://x.supabase.co",
                                   service_key="sk", session=sess)
    assert ids == [42]
    body = sess.post.call_args[1]["json"][0]
    assert body["metric_id"] == "gross_npl_ratio" and body["status"] == "pending"
    assert sess.post.call_args[1]["headers"]["Prefer"] == "return=representation"
