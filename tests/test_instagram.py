"""Posting card news to Instagram through the Graph API (mocked)."""
import copy
import io
import re
import time
from unittest import mock

import pytest
from PIL import Image

from paperagent import config, create_app, db, demo, instagram, storage


@pytest.fixture()
def ig(monkeypatch):
    monkeypatch.setattr(config, "INSTAGRAM_USER_ID", "1789")
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://starwars.example.com")


def _done_paper(cards=9):
    db.insert_paper({"uid": "arxiv:2610.01234", "source_kind": "arxiv", "source_name": "arXiv · cs.CL",
                     "title": "Self-Verification Loops", "url": "https://arxiv.org/abs/2610.01234"})
    pid = db.get_paper_by_uid("arxiv:2610.01234")["id"]
    keys = []
    for n in range(1, cards + 1):
        buf = io.BytesIO()
        Image.new("RGB", (1080, 1350), "white").save(buf, "PNG")
        keys.append(f"cards/{pid}/card_{n:02d}.png")
        storage.put(keys[-1], buf.getvalue())
    db.update_paper(pid, status="done", review=copy.deepcopy(demo.SAMPLE_REVIEW), card_paths=keys, score_overall=7.8)
    return pid


class FakeGraph:
    """Answers like the Graph API and records what was asked."""

    def __init__(self, status_codes=("IN_PROGRESS", "FINISHED")):
        self.calls, self.status = [], list(status_codes)

    def __call__(self, method, url, params=None, data=None, timeout=None):
        body = params or data
        self.calls.append((method, url, dict(body)))
        path = url.split("/v26.0/")[1]
        if method == "POST" and path == "1789/media" and body.get("is_carousel_item"):
            out = {"id": f"child{len(self.calls)}"}
        elif method == "POST" and path == "1789/media":
            out = {"id": "carousel1"}
        elif method == "GET" and path == "carousel1":
            out = {"status_code": self.status.pop(0)}
        elif path == "1789/media_publish":
            out = {"id": "17890000"}
        elif path == "17890000":
            out = {"permalink": "https://www.instagram.com/p/ABC123/"}
        else:
            out = {"error": {"message": f"unexpected {method} {path}"}}
        return mock.Mock(status_code=200, json=lambda: out)


def test_publish_posts_a_carousel_with_the_caption(fresh_db, ig):
    pid = _done_paper()
    graph = FakeGraph()
    with mock.patch.object(instagram.requests, "request", side_effect=graph), mock.patch.object(instagram.time, "sleep"):
        out = instagram.publish(db.get_paper(pid), "")
    assert out == {"media_id": "17890000", "permalink": "https://www.instagram.com/p/ABC123/"}
    children = [c for c in graph.calls if c[2].get("is_carousel_item")]
    assert len(children) == 9 and children[0][2]["image_url"] == f"https://starwars.example.com/files/card/{pid}/1.jpg"
    carousel = next(c for c in graph.calls if c[2].get("media_type") == "CAROUSEL")
    assert carousel[2]["children"].count(",") == 8
    caption = carousel[2]["caption"]
    assert caption.count("#") == 5 and "https://arxiv.org/abs/2610.01234" in caption
    assert all(c[2]["access_token"] == "tok" for c in graph.calls)
    assert graph.calls[0][1].startswith("https://graph.instagram.com/v26.0/")


def test_publish_needs_a_public_address(fresh_db, ig, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    pid = _done_paper()
    with pytest.raises(instagram.InstagramError, match="공개 https 주소"):
        instagram.publish(db.get_paper(pid), "http://127.0.0.1:8000/")


def test_processing_error_is_reported(fresh_db, ig):
    pid = _done_paper()
    with mock.patch.object(instagram.requests, "request", side_effect=FakeGraph(("ERROR",))), \
         mock.patch.object(instagram.time, "sleep"), pytest.raises(instagram.InstagramError, match="처리하지 못했습니다"):
        instagram.publish(db.get_paper(pid), "")


def test_button_posts_in_background_and_marks_the_paper(fresh_db, ig):
    pid = _done_paper()
    c = create_app(start_scheduler=False).test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    token = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").get_data(as_text=True)).group(1)
    assert "인스타에 바로 올리기" in c.get(f"/paper/{pid}").get_data(as_text=True)
    assert "인스타에 바로 올리기" in c.get("/").get_data(as_text=True)
    with mock.patch.object(instagram.requests, "request", side_effect=FakeGraph()), mock.patch.object(instagram.time, "sleep"):
        r = c.post(f"/paper/{pid}/instagram/publish", data={"_csrf": token}, headers={"Accept": "application/json"})
        assert r.get_json()["ok"]
        for _ in range(100):
            if db.get_paper(pid)["instagram_status"] != "publishing":
                break
            time.sleep(0.05)
    p = db.get_paper(pid)
    assert p["instagram_status"] == "posted" and p["instagram_posted_at"] and p["instagram_permalink"].endswith("/ABC123/")
    s = c.get(f"/api/paper/{pid}/instagram").get_json()
    assert s["status"] == "posted" and s["permalink"]
    html = c.get("/").get_data(as_text=True)
    assert 'class="pitem posted"' in html and "인스타에서 보기" in html


def test_button_explains_when_not_connected(fresh_db):
    pid = _done_paper()
    c = create_app(start_scheduler=False).test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    token = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").get_data(as_text=True)).group(1)
    assert "설정에서 인스타그램 계정을 연결하면" in c.get(f"/paper/{pid}").get_data(as_text=True)
    r = c.post(f"/paper/{pid}/instagram/publish", data={"_csrf": token}, headers={"Accept": "application/json"})
    assert r.status_code == 400 and "연결되지 않았습니다" in r.get_json()["error"]


def test_cards_are_served_as_jpeg(fresh_db):
    pid = _done_paper(cards=2)
    r = create_app(start_scheduler=False).test_client().get(f"/files/card/{pid}/1.jpg")
    assert r.status_code == 200 and r.mimetype == "image/jpeg" and Image.open(io.BytesIO(r.data)).size == (1080, 1350)
