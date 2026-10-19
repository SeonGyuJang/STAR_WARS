"""TECH card news: collectors (against saved pages and mocked APIs), the run
stage, and the dashboard pages."""
import copy
import io
import re
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from paperagent.collectors import tech

FIX = Path(__file__).parent / "fixtures"


class Resp:
    def __init__(self, text="", json=None, content=b"", headers=None):
        self.text, self._json, self.content = text, json, content or text.encode()
        self.headers = headers or {}

    def json(self):
        return self._json

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]


def test_github_trending_page_is_parsed():
    items = tech.parse_github_trending((FIX / "github_trending.html").read_text())
    assert [i["uid"] for i in items] == ["github:acme/agentkit", "github:someone/tiny-db"]
    a = items[0]
    assert a["title"] == "acme/agentkit" and a["url"] == "https://github.com/acme/agentkit"
    assert a["summary"].startswith("Build AI agents")
    assert a["meta"] == {"stars": 12345, "forks": 1020, "stars_today": 2310, "language": "Python"}
    assert items[1]["meta"]["stars_today"] == 95 and items[1]["meta"]["language"] is None


def test_github_falls_back_to_the_search_api():
    def fake(url, **kw):
        if "trending" in url:
            raise RuntimeError("403")
        assert "search/repositories" in url and kw["params"]["sort"] == "stars"
        return Resp(json={"items": [{"full_name": "New/Thing", "html_url": "https://github.com/New/Thing", "description": "d",
                                     "stargazers_count": 900, "forks_count": 10, "language": "Go", "topics": ["ai"]}]})

    with mock.patch.object(tech, "http_get", side_effect=fake):
        items = tech.github_trending()
    assert items[0]["uid"] == "github:new/thing" and items[0]["meta"]["new_this_week"] and items[0]["meta"]["stars"] == 900


def test_hacker_news_keeps_popular_stories():
    stories = {1: {"type": "story", "title": "Show HN: A thing", "url": "https://thing.dev", "score": 300, "descendants": 80, "by": "a"},
               2: {"type": "story", "title": "Low", "url": "https://x", "score": 20},
               3: {"type": "job", "title": "Hiring", "score": 500},
               4: {"type": "story", "title": "Ask HN: text post", "text": "<p>What do you use?</p>", "score": 150}}

    def fake(url, **kw):
        if url.endswith("topstories.json"):
            return Resp(json=[1, 2, 3, 4])
        return Resp(json=stories[int(re.search(r"/item/(\d+)", url).group(1))])

    with mock.patch.object(tech, "http_get", side_effect=fake):
        items = tech.hacker_news()
    assert [i["uid"] for i in items] == ["hn:1", "hn:4"]
    assert items[0]["meta"]["points"] == 300 and items[0]["meta"]["discussion"].endswith("id=1")
    assert items[1]["url"].endswith("item?id=4") and items[1]["summary"] == "What do you use?"


def test_geeknews_feed_is_parsed():
    items = tech.parse_geeknews_feed((FIX / "geeknews.xml").read_text())
    assert len(items) == 1
    g = items[0]
    assert g["uid"] == "geeknews:23456" and g["title"] == "오픈소스 음성 비서 Hoot 공개"
    assert "로컬에서 도는 음성 비서" in g["summary"] and g["meta"]["published"].startswith("2026-10-16")


def test_hugging_face_trending():
    def fake(url, **kw):
        assert kw["params"]["sort"] == "trendingScore"
        if url.endswith("/models"):
            return Resp(json=[{"id": "org/Model-7B", "likes": 1200, "downloads": 50000, "pipeline_tag": "text-generation", "trendingScore": 90}])
        return Resp(json=[{"id": "org/demo-space", "likes": 300, "sdk": "gradio", "trendingScore": 120}])

    with mock.patch.object(tech, "http_get", side_effect=fake):
        items = tech.hugging_face()
    assert [i["uid"] for i in items] == ["hf:space/org/demo-space", "hf:model/org/model-7b"]
    assert items[0]["url"] == "https://huggingface.co/spaces/org/demo-space" and items[1]["meta"]["task"] == "text-generation"


def test_one_failing_source_does_not_stop_the_others():
    log = []
    with mock.patch.dict(tech.COLLECTORS, {"github": mock.Mock(side_effect=RuntimeError("down")),
                                           "hn": mock.Mock(return_value=[{"uid": "hn:1"}])}):
        items = tech.collect(["github", "hn"], say=log.append)
    assert items == [{"uid": "hn:1"}] and any("failed" in x for x in log)


def test_readme_media_without_badges():
    readme = """
# Tool
[![build](https://img.shields.io/badge/build-passing.svg)](x) ![stars](https://api.star-history.com/svg?repos=a/b)
<p align="center"><img src="docs/hero.png" width="600"></p>
![demo](./assets/demo.gif)
https://github.com/user-attachments/assets/0f2c1d3e-1111-2222-3333-444455556666
![logo](https://example.com/logo.svg)
"""
    images, videos = tech.media_candidates(readme, "https://raw.githubusercontent.com/a/b/HEAD/")
    assert images == ["https://raw.githubusercontent.com/a/b/HEAD/docs/hero.png"]
    assert videos == ["https://raw.githubusercontent.com/a/b/HEAD/assets/demo.gif",
                      "https://github.com/user-attachments/assets/0f2c1d3e-1111-2222-3333-444455556666"]


def test_compact_numbers():
    assert tech.compact(980) == "980" and tech.compact(2310) == "2,310" and tech.compact(12840) == "1.3만"
    assert tech.compact(50000) == "5만" and tech.compact(None) == "-"


def _png(w=1200, h=630, color="#335"):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_tech_stage_end_to_end(fresh_db):
    """Collect -> screen -> context and media -> write -> cards (with a video slide) -> pages."""
    from paperagent import agent, create_app, db, demo, pipeline, technews

    items = [
        {**demo.SAMPLE_TECH, "uid": "github:facebookresearch/agentkit", "title": "facebookresearch/agentkit",
         "url": "https://github.com/facebookresearch/agentkit"},
        {"uid": "hn:99", "source": "hn", "title": "Celebrity drama", "url": "https://gossip.example", "summary": "", "meta": {"points": 500}},
    ]

    def screen(pending, meter=None):
        return {p["id"]: (8.5, "개발자 관심이 큼") if p["source"] == "github" else (1.0, "가십") for p in pending}

    gif = Path(demo.make_tech_media(Path(db.config.DATA_DIR))[1]).read_bytes()

    def download(url, **kw):
        if url.endswith(".gif"):
            return Resp(content=gif, headers={"Content-Type": "image/gif"})
        if "tiny" in url:
            return Resp(content=_png(200, 100), headers={"Content-Type": "image/png"})
        return Resp(content=_png(), headers={"Content-Type": "image/png"})

    context = {"text": "# agentkit\nInstall: pip install agentkit", "images": ["https://x/tiny.png", "https://x/hero.png"],
               "videos": ["https://x/demo.gif"]}
    with mock.patch.object(tech, "collect", return_value=items), \
            mock.patch.object(agent, "screen_tech", side_effect=screen), \
            mock.patch.object(tech, "fetch_context", return_value=context), \
            mock.patch.object(technews, "http_get", side_effect=download), \
            mock.patch.object(agent, "write_tech", return_value=copy.deepcopy(demo.SAMPLE_TECH_REVIEW)), \
            mock.patch.object(agent, "ensure_api"):
        run_id = pipeline.run("manual", tech_only=True)

    run = db.get_run(run_id)
    assert run["status"] == "ok" and run["stats"]["tech_done"] == 1, run["log"]
    done = db.get_tech_by_uid("github:facebookresearch/agentkit")
    assert done["status"] == "done" and done["interest"] == 8.5
    assert db.get_tech_by_uid("hn:99")["status"] == "skipped"
    assert len(done["media"]["images"]) == 1  # the 200px picture was too small
    assert done["media"]["video"].endswith("demo.gif")
    # cover, what, demo, how, so-what, try, summary, closing - no separate numbers slide
    assert len(done["card_paths"]) == 8 and done["card_paths"][0] == f"tech/{done['id']}/cards/card_01.png"
    from paperagent import video

    if video.available():
        assert sorted(done["card_videos"]) == ["3", "8"]  # the demo clip (it shows what the post is about) and the closing slide

    app = create_app(start_scheduler=False)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    html = c.get("/tech").get_data(as_text=True)
    assert "터미널 안에서 일하는 AI 코딩 도우미" in html and "1.3만" in html and 'class="sbadge s-github"' in html
    assert 'class="mi maker"><svg class="logo-mark"' in html and 'aria-label="Meta"' in html  # facebookresearch -> Meta's logo
    page = c.get(f"/tech/{done['id']}").get_data(as_text=True)
    assert "그래서 나랑 무슨 상관?" in page and "cargo install termpilot" in page and "data-zoom" in page  # full-screen viewer and "#IT #테크 #개발자 #오픈소스 #코딩" in page
    assert c.get(f"/files/tech/{done['id']}/card/1").status_code == 200
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/files/tech/{done['id']}/cards.zip").data))
    names = z.namelist()
    assert "caption.txt" in names and len([n for n in names if n.startswith("card_")]) == 8
    if video.available():
        assert {"card_03.mp4", "card_08.mp4"} <= set(names) and "card_02.png" in names and "card_08.png" not in names
        assert c.get(f"/files/tech/{done['id']}/card/8.mp4").mimetype == "video/mp4"
        assert "<video" in page
    # the home page points to the new TECH post
    assert "새 테크 소식" in c.get("/").get_data(as_text=True)


def test_tech_settings_and_manual_pick(fresh_db):
    from paperagent import create_app, db, pipeline

    app = create_app(start_scheduler=False)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    token = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/settings").get_data(as_text=True)).group(1)
    r = c.post("/settings/tech", data={"_csrf": token, "tech_per_run": "3", "tech_min_interest": "6.5", "src_hn": "1", "src_hf": "1"})
    assert r.status_code == 302
    s = db.pipeline_settings()
    assert s["tech_per_run"] == 3 and s["tech_min_interest"] == 6.5 and s["tech_sources"] == ["hn", "hf"]
    assert "GeekNews" in c.get("/tech").get_data(as_text=True)  # still listed, marked off

    db.insert_tech({"uid": "hn:5", "source": "hn", "title": "Skipped but good", "url": "https://x", "meta": {}})
    tid = db.get_tech_by_uid("hn:5")["id"]
    db.update_tech(tid, status="skipped", interest=5, interest_reason="애매함")
    queue = c.get("/queue?kind=tech").get_data(as_text=True)
    assert "Skipped but good" in queue and "흥미도 미달" in queue and 'class="on"><span class="sdot s-tech">' in queue
    assert "Skipped but good" not in c.get("/tech").get_data(as_text=True)  # the list shows finished posts only

    # pick it for the next run: it goes first in the plan
    c.post("/queue/tech", data={"_csrf": token, "item_id": str(tid), "when": "next"})
    assert db.get_tech(tid)["status"] == "screened" and db.get_tech(tid)["priority"] == 1
    plan = c.get("/queue?kind=tech").get_data(as_text=True)
    assert plan.index("다음 실행에서 만들 소식") < plan.index("Skipped but good") and "직접 선택" in plan
    # set it aside again
    c.post("/queue/tech", data={"_csrf": token, "item_id": str(tid), "when": "skip"})
    assert db.get_tech(tid)["status"] == "skipped" and db.get_tech(tid)["priority"] == -1
    # make it now
    with mock.patch.object(pipeline, "run_in_background", return_value=True) as bg, \
            mock.patch("paperagent.agent.api_status", return_value={"ok": True}):
        c.post("/queue/tech", data={"_csrf": token, "item_id": str(tid), "when": "now"})
    assert bg.call_args.kwargs == {"tech_only": True, "tech_ids": [tid]}

    visitor = app.test_client()
    visitor.environ_base["REMOTE_ADDR"] = "10.0.0.9"
    page = visitor.get("/queue?kind=tech").get_data(as_text=True)
    assert "Skipped but good" in page and 'name="item_id"' not in page  # visitors see the queue, not the controls
    assert visitor.post("/queue/tech", data={"_csrf": token, "item_id": str(tid)}).status_code in (302, 400)


def test_context_from_readme_and_article():
    readme = "# agentkit\n![hero](docs/hero.png)\n![demo](https://cdn.example.com/demo.mp4)\nRun `pip install agentkit`."
    article = ('<html><head><meta property="og:image" content="/img/cover.jpg"></head><body><nav>menu</nav>'
               '<article><h1>Big launch</h1><p>The team released version 2 today.</p></article></body></html>')

    def fake(url, **kw):
        if url == "https://raw.githubusercontent.com/acme/agentkit/HEAD/README.md":
            return Resp(text=readme)
        if url == "https://blog.example.com/launch":
            return Resp(text=article)
        raise RuntimeError("404")

    with mock.patch.object(tech, "http_get", side_effect=fake):
        gh = tech.fetch_context({"uid": "github:acme/agentkit", "source": "github", "title": "acme/agentkit", "meta": {}})
        hn = tech.fetch_context({"uid": "hn:1", "source": "hn", "title": "Big launch", "url": "https://blog.example.com/launch",
                                 "summary": "", "meta": {}})
    assert "pip install agentkit" in gh["text"]
    assert gh["images"] == ["https://raw.githubusercontent.com/acme/agentkit/HEAD/docs/hero.png",
                            "https://opengraph.githubassets.com/1/acme/agentkit"]
    assert gh["videos"] == ["https://cdn.example.com/demo.mp4"]
    assert "released version 2" in hn["text"] and "menu" not in hn["text"]
    assert hn["images"][0] == "https://blog.example.com/img/cover.jpg"


def test_makers_and_logos():
    from paperagent import logos

    assert logos.maker_for({"source": "github", "title": "facebookresearch/sam"})["logo"]["slug"] == "meta"
    assert logos.maker_for({"source": "hf", "title": "google/gemma-4"})["name"] == "Google"
    assert logos.maker_for({"source": "hn", "title": "x"}, {"maker": "Apple"})["logo"]["slug"] == "apple"
    assert logos.maker_for({"source": "hn", "title": "x"}, {"maker": "Instagram"})["logo"]["hex"]
    openai = logos.maker_for({"source": "github", "title": "openai/codex"})
    assert openai["name"] == "OpenAI" and openai["logo"] is None  # no official mark in the set: name only
    assert logos.maker_for({"source": "hn", "title": "x"}, {"maker": ""}) is None
    assert 'fill="#111111"' in str(logos.svg(logos.find("Hugging Face")))  # too light on white -> dark


def test_demo_clip_only_when_it_shows_the_post(tmp_path):
    """The project's own clip becomes a video slide only when the writer says it shows what the post is about."""
    from paperagent import cardnews, demo, video

    if not video.ffmpeg_exe():
        pytest.skip("ffmpeg not available")
    banner, gif = demo.make_tech_media(tmp_path)
    item = {**demo.SAMPLE_TECH, "id": 1}
    media = {"images": [str(banner)], "video": str(gif)}
    review = copy.deepcopy(demo.SAMPLE_TECH_REVIEW)
    slides, _ = cardnews.tech_slides(item, review, media, tmp_path)
    assert [s.get("video") is not None for s in slides if s["kind"] == "media"] == [True]
    assert "numbers" not in [s["kind"] for s in slides]
    review["demo"]["use_clip"] = False
    slides, _ = cardnews.tech_slides(item, review, media, tmp_path)
    media_slide = next(s for s in slides if s["kind"] == "media")
    assert "video" not in media_slide and media_slide["figure"]  # a still from the clip instead


GEEKNEWS_TOPIC = """<html><body><div class="topic">
<div class="topictitle"><a href="https://github.com/facebookresearch/hoot" class="ud"><h1>오픈소스 음성 비서 Hoot 공개</h1></a>
<span class="topicurl">(github.com/facebookresearch)</span></div>
<div class="topicinfo">10 points by xguru 2시간전</div>
<div id="topic_contents"><ul><li>로컬에서 도는 음성 비서</li><li>MIT 라이선스</li></ul></div>
</div><a href="https://example.com/ad">광고</a></body></html>"""


def test_geeknews_reads_the_original_it_points_to():
    """GeekNews has only a short summary: the original (here a GitHub repo) is read for text, pictures and clips."""
    readme = "# Hoot\nA local voice assistant.\n![screen](docs/screen.png)\n## Demo\nTalking to it:\n![demo](docs/demo.gif)"

    def fake(url, **kw):
        if url == "https://news.hada.io/topic?id=23456":
            return Resp(text=GEEKNEWS_TOPIC)
        if url == "https://raw.githubusercontent.com/facebookresearch/hoot/HEAD/README.md":
            return Resp(text=readme)
        raise RuntimeError(f"unexpected {url}")

    item = {"uid": "geeknews:23456", "source": "geeknews", "title": "오픈소스 음성 비서 Hoot 공개",
            "url": "https://news.hada.io/topic?id=23456", "summary": "로컬 음성 비서",
            "meta": {"discussion": "https://news.hada.io/topic?id=23456"}}
    with mock.patch.object(tech, "http_get", side_effect=fake):
        ctx = tech.fetch_context(item)
    assert item["url"] == "https://github.com/facebookresearch/hoot"  # the original, not the GeekNews page
    assert "GeekNews 요약" in ctx["text"] and "MIT 라이선스" in ctx["text"] and "A local voice assistant" in ctx["text"]
    assert ctx["images"][0] == "https://raw.githubusercontent.com/facebookresearch/hoot/HEAD/docs/screen.png"
    assert ctx["videos"] == ["https://raw.githubusercontent.com/facebookresearch/hoot/HEAD/docs/demo.gif"]
    assert "Talking to it" in ctx["clip_notes"][ctx["videos"][0]]
    from paperagent import logos

    assert logos.maker_for(item)["name"] == "Meta"  # the repository's organisation


def test_geeknews_origin_without_the_usual_markup():
    html = ('<html><body><nav><a href="https://news.hada.io/">GeekNews</a></nav><h2>제목</h2>'
            '<p><a href="https://blog.example.com/post">원문</a></p><div id="topic_contents">요약</div>'
            '<footer><a href="https://twitter.com/x">x</a></footer></body></html>')
    assert tech.geeknews_origin(html) == ("https://blog.example.com/post", "요약")


def test_news_reads_everything_behind_it():
    """GeekNews -> a blog post (the original) -> the GitHub project it introduces: all three are read, and the
    pictures and clips of the original and the repository can be used on the cards."""
    topic = """<html><body><div class="topictitle"><a href="https://blog.example.com/hoot-launch"><h1>Hoot 공개</h1></a></div>
      <div id="topic_contents"><p>로컬 음성 비서 Hoot이 공개됨.</p><p>저장소: <a href="https://github.com/facebookresearch/hoot">github</a></p></div></body></html>"""
    article = """<html><head><meta property="og:image" content="https://blog.example.com/cover.jpg"></head>
      <body><article><h1>Introducing Hoot</h1><p>Hoot answers in 120 ms on a laptop.</p>
      <p>Code: <a href="https://github.com/facebookresearch/hoot">facebookresearch/hoot</a></p></article></body></html>"""
    readme = "# Hoot\\nInstall with pip install hoot.\\n![ui](docs/ui.png)\\n## Demo\\nAsking for the weather:\\n![demo](docs/demo.mp4)"

    def fake(url, **kw):
        pages = {"https://news.hada.io/topic?id=1": topic, "https://blog.example.com/hoot-launch": article,
                 "https://raw.githubusercontent.com/facebookresearch/hoot/HEAD/README.md": readme}
        if url in pages:
            return Resp(text=pages[url])
        raise RuntimeError(f"unexpected {url}")

    item = {"uid": "geeknews:1", "source": "geeknews", "title": "Hoot 공개", "url": "https://news.hada.io/topic?id=1",
            "summary": "", "meta": {"discussion": "https://news.hada.io/topic?id=1"}}
    with mock.patch.object(tech, "http_get", side_effect=fake):
        ctx = tech.fetch_context(item)
    text = ctx["text"]
    assert "GeekNews 요약" in text and "로컬 음성 비서" in text  # the news summary
    assert "원문 (https://blog.example.com/hoot-launch)" in text and "120 ms" in text  # the original article
    assert "facebookresearch/hoot" in text and "pip install hoot" in text  # the project's README
    assert item["url"] == "https://blog.example.com/hoot-launch" and item["meta"]["repo"] == "https://github.com/facebookresearch/hoot"
    base = "https://raw.githubusercontent.com/facebookresearch/hoot/HEAD/docs/"
    assert ctx["images"][:2] == ["https://blog.example.com/cover.jpg", base + "ui.png"]
    assert ctx["videos"] == [base + "demo.mp4"] and "weather" in ctx["clip_notes"][base + "demo.mp4"]
    from paperagent import logos

    assert logos.maker_for(item)["name"] == "Meta"


def test_one_failing_original_keeps_the_rest():
    topic = """<div class="topictitle"><a href="https://down.example.com/x">t</a></div>
      <div id="topic_contents">요약 <a href="https://github.com/acme/tool">repo</a></div>"""

    def fake(url, **kw):
        if url == "https://news.hada.io/topic?id=2":
            return Resp(text=topic)
        if url == "https://raw.githubusercontent.com/acme/tool/HEAD/README.md":
            return Resp(text="# tool\\nDoes things.")
        raise RuntimeError("down")

    item = {"uid": "geeknews:2", "source": "geeknews", "title": "t", "url": "", "summary": "",
            "meta": {"discussion": "https://news.hada.io/topic?id=2"}}
    with mock.patch.object(tech, "http_get", side_effect=fake):
        ctx = tech.fetch_context(item)
    assert "요약" in ctx["text"] and "Does things." in ctx["text"]  # the article was down; the summary's repo still read


def test_hugging_face_follows_the_code_repository():
    card = "---\nlicense: mit\n---\n# Tiny Vision\nCode: https://github.com/acme/tiny-vision\n![sample](sample.png)"
    readme = "# tiny-vision\nRun it in the browser.\n![demo](demo.gif)"

    def fake(url, **kw):
        pages = {"https://huggingface.co/acme/tiny-vision/raw/main/README.md": card,
                 "https://raw.githubusercontent.com/acme/tiny-vision/HEAD/README.md": readme}
        if url in pages:
            return Resp(text=pages[url])
        raise RuntimeError(f"unexpected {url}")

    item = {"uid": "hf:model/acme/tiny-vision", "source": "hf", "title": "acme/tiny-vision",
            "url": "https://huggingface.co/acme/tiny-vision", "summary": "", "meta": {"kind": "model"}}
    with mock.patch.object(tech, "http_get", side_effect=fake):
        ctx = tech.fetch_context(item)
    assert "Tiny Vision" in ctx["text"] and "Run it in the browser" in ctx["text"]
    assert ctx["images"][0] == "https://huggingface.co/acme/tiny-vision/resolve/main/sample.png"
    assert ctx["videos"] == ["https://raw.githubusercontent.com/acme/tiny-vision/HEAD/demo.gif"]
    assert item["meta"]["repo"] == "https://github.com/acme/tiny-vision"


def test_tech_read_again(fresh_db):
    from paperagent import create_app, db, pipeline

    db.insert_tech({"uid": "hn:7", "source": "hn", "title": "A post", "url": "https://x", "meta": {}})
    tid = db.get_tech_by_uid("hn:7")["id"]
    db.update_tech(tid, status="done", review={"title_ko": "글", "one_liner": "한 줄", "summary": "요약", "caption": "c",
                                               "_read": [{"label": "원문 (https://x)", "chars": 12345}],
                                               "_unread": [{"label": "GitHub 저장소 acme/x", "why": "HTTP 404 (raw.githubusercontent.com)"}]})
    app = create_app(start_scheduler=False)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    page = c.get(f"/tech/{tid}").get_data(as_text=True)
    assert "다시 읽기" in page and f"/tech/{tid}/reprocess" in page
    assert "읽은 자료" in page and "12,345자" in page and "못 읽음 · HTTP 404" in page  # what the post was written from
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    with mock.patch.object(pipeline, "run_in_background", return_value=True) as bg, \
            mock.patch("paperagent.agent.api_status", return_value={"ok": True}):
        r = c.post(f"/tech/{tid}/reprocess", data={"_csrf": token})
    assert r.status_code == 302 and bg.call_args.kwargs == {"tech_only": True, "tech_ids": [tid]}
    visitor = app.test_client()
    visitor.environ_base["REMOTE_ADDR"] = "10.0.0.9"
    assert "다시 읽기" not in visitor.get(f"/tech/{tid}").get_data(as_text=True)


def _hn_api(story_id, kids, comments):
    """Fake Hacker News API answers for a story and its comments."""
    pages = {f"{tech.HN_API}/item/{story_id}.json": {"id": story_id, "kids": kids}}
    pages.update({f"{tech.HN_API}/item/{k}.json": c for k, c in zip(kids, comments)})
    return pages


def test_hacker_news_reads_the_linked_article_not_just_the_listing():
    """Hacker News only carries a title: the article it links to is read (asked for like a browser, since many sites turn
    bots away), even when the story sits in plain <div>s among menus and teasers; readers' comments come after it."""
    article = """<html><head><meta property="og:image" content="/og.png"></head><body>
      <div class="menu"><a href="/">Home</a> <a href="/blog">Blog</a></div>
      <div class="teaser"><p>Subscribe to our newsletter for weekly updates.</p></div>
      <div class="wrap"><div class="story">
        <h2>Why we rewrote our database in Rust</h2>
        <p>Last year our write path spent 40 percent of its time in garbage collection pauses under load.</p>
        <p>After the rewrite, p99 latency fell from 180 ms to 12 ms on the same hardware, and memory use halved.</p>
        <p>The migration took nine months and was done table by table behind a feature flag.</p>
        <ul><li>Zero downtime during the cut-over</li><li>Open-sourced the storage engine as quarry</li></ul>
      </div></div><footer>© 2026</footer></body></html>"""
    seen = {}
    api = _hn_api(4242, [1, 2, 3], [{"text": "Nine months seems fast for a rewrite."}, {"deleted": True},
                                     {"text": "Did they benchmark against tuning the GC first?"}])

    def fake(url, **kw):
        if url == "https://db.example.com/rust-rewrite":
            seen["headers"] = kw.get("headers")
            return Resp(text=article, headers={"Content-Type": "text/html"})
        if url in api:
            return Resp(json=api[url])
        raise RuntimeError(f"unexpected {url}")

    item = {"uid": "hn:4242", "source": "hn", "title": "Why we rewrote our database in Rust",
            "url": "https://db.example.com/rust-rewrite", "summary": "", "meta": {"points": 640}}
    with mock.patch.object(tech, "http_get", side_effect=fake):
        ctx = tech.fetch_context(item)
    text = ctx["text"]
    assert "Mozilla/5.0" in seen["headers"]["User-Agent"]
    assert "p99 latency fell from 180 ms to 12 ms" in text and "table by table" in text and "Zero downtime" in text
    assert "Subscribe to our newsletter" not in text and "Home" not in text.split("Hacker News 댓글")[0]
    assert text.index("원문 (https://db.example.com/rust-rewrite)") < text.index("Hacker News 댓글")
    assert "tuning the GC" in text and ctx["images"][0] == "https://db.example.com/og.png"
    assert ctx["original_chars"] > 300 and ctx["failed"] == []
    assert [label for label, _ in ctx["read"]] == ["원문 (https://db.example.com/rust-rewrite)", "Hacker News 댓글"]


def test_original_drawn_by_javascript_is_rendered():
    shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    rendered = "<html><body><main>" + "".join(f"<p>Paragraph {i}: the new compiler cuts build times by half.</p>" for i in range(30)) \
               + "</main></body></html>"

    def fake(url, **kw):
        if url == "https://app.example.com/post":
            return Resp(text=shell, headers={"Content-Type": "text/html"})
        raise RuntimeError("no comments")

    item = {"uid": "hn:5", "source": "hn", "title": "New compiler", "url": "https://app.example.com/post", "summary": "", "meta": {}}
    with mock.patch.object(tech, "http_get", side_effect=fake), \
            mock.patch.object(tech, "render_page", return_value=rendered) as render:
        ctx = tech.fetch_context(item)
    render.assert_called_once_with("https://app.example.com/post")
    assert "cuts build times by half" in ctx["text"] and ctx["original_chars"] > 800


def test_original_that_is_a_pdf():
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Quarry: a storage engine. Code at https://github.com/acme/quarry")
    pdf = doc.tobytes()

    def fake(url, **kw):
        if url == "https://acme.example.com/paper.pdf":
            return Resp(content=pdf, headers={"Content-Type": "application/pdf"})
        if url == "https://raw.githubusercontent.com/acme/quarry/HEAD/README.md":
            return Resp(text="# quarry\nEmbedded storage engine.")
        raise RuntimeError("down")

    item = {"uid": "hn:6", "source": "hn", "title": "Quarry", "url": "https://acme.example.com/paper.pdf", "summary": "", "meta": {}}
    with mock.patch.object(tech, "http_get", side_effect=fake):
        ctx = tech.fetch_context(item)
    assert "Quarry: a storage engine" in ctx["text"] and "Embedded storage engine" in ctx["text"]
    assert item["meta"]["repo"] == "https://github.com/acme/quarry"


def test_no_hollow_post_when_the_original_cannot_be_read(fresh_db):
    """An original that cannot be read leaves only the title and comments: the item is marked with the reason
    (to read again later) instead of becoming card news about nothing."""
    from paperagent import agent, technews

    db = fresh_db
    db.insert_tech({"uid": "hn:8", "source": "hn", "title": "Blocked article", "url": "https://blocked.example.com/a", "meta": {}})
    item = db.get_tech_by_uid("hn:8")
    api = _hn_api(8, [1], [{"text": "Interesting."}])

    def fake(url, **kw):
        if url in api:
            return Resp(json=api[url])
        import requests

        resp = mock.Mock(status_code=403, url=url)
        raise requests.HTTPError("403", response=resp)

    lines = []
    with mock.patch.object(tech, "http_get", side_effect=fake), mock.patch.object(agent, "write_tech") as write:
        ok, _ = technews.process_item(item, say=lines.append)
    assert not ok and not write.called
    after = db.get_tech(item["id"])
    assert after["status"] == "error" and "원문을 읽지 못해" in after["error"] and "HTTP 403 (blocked.example.com)" in after["error"]
    assert any("could not read 원문" in line for line in lines)
    # GeekNews still has its Korean summary when the original is down: that is enough to write from
    assert technews.enough_material({"original_chars": 0, "read": [("GeekNews 요약", 900)]})
    assert not technews.enough_material({"original_chars": 0, "read": [("Hacker News 댓글", 5000)]})
    assert technews.enough_material({"original_chars": None, "read": []})  # Ask HN: the post itself is the material


ARXIV_ABS = """<html><head><meta name="citation_title" content="Hybrid Reasoning Agents">
<meta name="citation_author" content="Kim, Ana"><meta name="citation_author" content="Lee, Ben">
<meta name="citation_abstract" content="Agents that think in latent space and act through text cut tool-use errors by 31%.">
</head><body><blockquote class="abstract">Abstract: ignored when the meta tag is there</blockquote></body></html>"""
ARXIV_HTML = """<html><body><div class="ltx_page_main"><article class="ltx_document">
<h1 class="ltx_title">Hybrid Reasoning Agents</h1>
<section><h2>1 Introduction</h2><p>Language agents usually reason in text, which is slow and verbose for planning steps.</p>
<p>We let the model plan in its hidden states and only write text when it calls a tool or answers.</p></section>
<figure class="ltx_figure"><img src="x1.png" alt="overview"><figcaption>Figure 1: Overview of the agent loop.</figcaption></figure>
<section><h2>4 Results</h2><p>On WebArena the success rate rises from 38.2% to 46.9% while using 3x fewer tokens.</p></section>
</article></div></body></html>"""


def test_hacker_news_link_to_an_arxiv_paper():
    """The run that failed: an HN story pointing at arxiv.org/abs. The paper's abstract and full text (HTML version) are
    read, with its figures for the cards."""
    seen = []

    def fake(url, **kw):
        seen.append(url)
        if url == "https://arxiv.org/abs/2610.18861":
            return Resp(text=ARXIV_ABS, headers={"Content-Type": "text/html"})
        if url == "https://arxiv.org/html/2610.18861":
            return Resp(text=ARXIV_HTML, headers={"Content-Type": "text/html"})
        if url.startswith(tech.HN_API):
            return Resp(json={"kids": []})
        raise RuntimeError(f"unexpected {url}")

    item = {"uid": "hn:70", "source": "hn", "title": "Hybrid Reasoning Agents: LLM agents that think in latent space, act through text",
            "url": "https://arxiv.org/abs/2610.18861", "summary": "", "meta": {"points": 300}}
    with mock.patch.object(tech, "http_get", side_effect=fake):
        ctx = tech.fetch_context(item)
    text = ctx["text"]
    assert ctx["failed"] == [] and ctx["original_chars"] > 300
    assert "논문 제목: Hybrid Reasoning Agents" in text and "저자: Kim, Ana, Lee, Ben" in text and "31%" in text
    assert "38.2% to 46.9%" in text and "hidden states" in text
    assert ctx["images"] == ["https://arxiv.org/html/2610.18861/x1.png"]
    from paperagent import technews

    assert technews.enough_material(ctx)


def test_arxiv_without_an_html_version_reads_the_pdf():
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "We let the model plan in its hidden states.")
    pdf = doc.tobytes()

    def fake(url, **kw):
        if url == "https://arxiv.org/abs/2610.18861":
            return Resp(text=ARXIV_ABS)
        if url == "https://arxiv.org/pdf/2610.18861":
            return Resp(content=pdf, headers={"Content-Type": "application/pdf"})
        raise RuntimeError("404")

    with mock.patch.object(tech, "http_get", side_effect=fake):
        text, images, *_ = tech.read_source("https://arxiv.org/pdf/2610.18861v2")
    assert "31%" in text and "plan in its hidden states" in text and images == []


def test_reading_never_depends_on_css_selectors(monkeypatch):
    """The live run failed inside the CSS selector library ('Invalid character ... article'). Page reading, the GitHub
    Trending page and GeekNews topics are read without CSS selectors, so such a failure cannot stop them."""
    from bs4.element import Tag

    def broken(*a, **k):
        raise ValueError("Invalid character 'a' position 0")

    monkeypatch.setattr(Tag, "select", broken)
    monkeypatch.setattr(Tag, "select_one", broken)
    text, _ = tech._page_text(ARXIV_HTML)
    assert "46.9%" in text
    assert tech.parse_github_trending((FIX / "github_trending.html").read_text())  # still parsed
    origin, summary = tech.geeknews_origin('<div class="topictitle"><a href="https://blog.example.com/x">t</a></div>'
                                           '<div id="topic_contents"><p>요약 내용</p></div>')
    assert origin == "https://blog.example.com/x" and "요약 내용" in summary


def test_short_error_is_one_line():
    from paperagent.collectors import short_error

    assert short_error(ValueError("Invalid character 'a' position 0\n  line 1:\narticle\n^")) == \
        "Invalid character 'a' position 0 line 1: article ^"


GN_FEED = """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>T3 Code - 여러 AI 코딩 에이전트를 관리하는 앱</title>
<link rel="alternate" type="text/html" href="https://news.hada.io/topic?id=96"/><id>https://news.hada.io/topic?id=96</id>
<content type="html">&lt;p&gt;&lt;a href="https://t3.codes/"&gt;T3 Code&lt;/a&gt;는 여러 코딩 에이전트를 한 화면에서 관리하는 앱.&lt;/p&gt;
&lt;p&gt;저장소: &lt;a href="https://github.com/pingdotgg/t3code"&gt;GitHub&lt;/a&gt;&lt;/p&gt;</content></entry></feed>"""


def test_geeknews_topic_closed_to_us_still_reaches_the_original():
    """news.hada.io answered 403 in the live run. The topic page is then opened in a headless browser; if that fails too,
    the feed's copy of the summary and the links in it lead to the original and its repository."""
    item = tech.parse_geeknews_feed(GN_FEED)[0]
    assert item["meta"]["links"] == ["https://t3.codes/", "https://github.com/pingdotgg/t3code"]
    landing = "<html><body><main>" + "".join(f"<p>T3 Code runs Claude Code, Codex and others side by side ({i}).</p>"
                                             for i in range(20)) + "</main></body></html>"

    def fake(url, **kw):
        if url == "https://news.hada.io/topic?id=96":
            import requests

            raise requests.HTTPError("403", response=mock.Mock(status_code=403, url=url))
        if url == "https://t3.codes/":
            return Resp(text=landing, headers={"Content-Type": "text/html"})
        if url == "https://raw.githubusercontent.com/pingdotgg/t3code/HEAD/README.md":
            return Resp(text="# T3 Code\nA desktop and mobile app for coding agents.\n![shot](docs/shot.png)")
        raise RuntimeError(f"unexpected {url}")

    with mock.patch.object(tech, "http_get", side_effect=fake), mock.patch.object(tech, "render_page", return_value=None) as render:
        ctx = tech.fetch_context(item)
    render.assert_called_once_with("https://news.hada.io/topic?id=96")  # tried a real browser first
    assert ctx["failed"] == [("GeekNews 글", "HTTP 403 (news.hada.io)")]
    assert "여러 코딩 에이전트" in ctx["text"] and "side by side" in ctx["text"] and "desktop and mobile app" in ctx["text"]
    assert item["url"] == "https://t3.codes/" and item["meta"]["repo"] == "https://github.com/pingdotgg/t3code"
    assert ctx["original_chars"] > 300 and "https://raw.githubusercontent.com/pingdotgg/t3code/HEAD/docs/shot.png" in ctx["images"]


def test_geeknews_topic_opened_in_a_browser_when_refused():
    topic = """<html><body><div class="topictitle"><a href="https://blog.example.com/md5">원문</a></div>
      <div id="topic_contents"><p>""" + "에이전트가 수정했다고 답했지만 파일은 그대로였던 사례. " * 30 + "</p></div></body></html>"
    article = "<html><body><article>" + "<p>The agent said it fixed the file, but the checksum never changed.</p>" * 20 + "</article></body></html>"

    def fake(url, **kw):
        if "hada.io" in url:
            import requests

            raise requests.HTTPError("403", response=mock.Mock(status_code=403, url=url))
        if url == "https://blog.example.com/md5":
            return Resp(text=article)
        raise RuntimeError(f"unexpected {url}")

    item = {"uid": "geeknews:99", "source": "geeknews", "title": "md5", "url": "https://news.hada.io/topic?id=99", "summary": "",
            "meta": {"discussion": "https://news.hada.io/topic?id=99"}}
    with mock.patch.object(tech, "http_get", side_effect=fake), mock.patch.object(tech, "render_page", return_value=topic):
        ctx = tech.fetch_context(item)
    assert ctx["failed"] == [] and "checksum never changed" in ctx["text"] and "그대로였던 사례" in ctx["text"]
    assert item["url"] == "https://blog.example.com/md5"


def test_geeknews_with_nothing_but_a_short_feed_summary_is_not_written(fresh_db):
    from paperagent import agent, technews

    db = fresh_db
    db.insert_tech({"uid": "geeknews:62", "source": "geeknews", "title": "Ultramarine 1 Flash", "url": "https://news.hada.io/topic?id=62",
                    "summary": "금융, 법률용 오픈 모델", "meta": {"discussion": "https://news.hada.io/topic?id=62", "links": []}})
    item = db.get_tech_by_uid("geeknews:62")

    def fake(url, **kw):
        import requests

        raise requests.HTTPError("403", response=mock.Mock(status_code=403, url=url))

    with mock.patch.object(tech, "http_get", side_effect=fake), mock.patch.object(agent, "write_tech") as write:
        ok, _ = technews.process_item(item, say=lambda *a: None)
    assert not ok and not write.called
    assert "원문을 읽지 못해" in db.get_tech(item["id"])["error"] and "HTTP 403 (news.hada.io)" in db.get_tech(item["id"])["error"]


def test_the_failing_run_again(fresh_db):
    """The run from the report, replayed: the arXiv story is written from the paper, the GeekNews post whose topic page
    is closed is written from the original its feed links to, and the one with nothing readable is held back."""
    from paperagent import agent, demo, technews

    db = fresh_db
    hn = {"uid": "hn:70", "source": "hn", "title": "Hybrid Reasoning Agents", "url": "https://arxiv.org/abs/2610.18861",
          "summary": "", "meta": {"points": 300}}
    gn = tech.parse_geeknews_feed(GN_FEED)[0]
    weak = {"uid": "geeknews:62", "source": "geeknews", "title": "Ultramarine 1 Flash", "url": "https://news.hada.io/topic?id=62",
            "summary": "금융용 오픈 모델", "meta": {"discussion": "https://news.hada.io/topic?id=62", "links": []}}
    for it in (hn, gn, weak):
        db.insert_tech(it)
    landing = "<html><body><main>" + "<p>T3 Code runs several coding agents side by side on desktop and phone.</p>" * 20 + "</main></body></html>"

    def fake(url, **kw):
        import requests

        pages = {"https://arxiv.org/abs/2610.18861": ARXIV_ABS, "https://arxiv.org/html/2610.18861": ARXIV_HTML,
                 "https://t3.codes/": landing,
                 "https://raw.githubusercontent.com/pingdotgg/t3code/HEAD/README.md": "# T3 Code\nCoding agents app."}
        if url in pages:
            return Resp(text=pages[url], headers={"Content-Type": "text/html"})
        if url.startswith(tech.HN_API):
            return Resp(json={"kids": []})
        if "hada.io" in url:
            raise requests.HTTPError("403", response=mock.Mock(status_code=403, url=url))
        raise requests.ConnectionError(f"no {url}")

    written = []

    def write(item, context, meter=None):
        written.append((item["uid"], context["text"]))
        return copy.deepcopy(demo.SAMPLE_TECH_REVIEW)

    lines = []
    with mock.patch.object(tech, "http_get", side_effect=fake), mock.patch.object(technews, "http_get", side_effect=fake), \
            mock.patch.object(agent, "write_tech", side_effect=write), \
            mock.patch.object(technews.cardnews, "render_tech_and_store", return_value=([], {})):
        stats = technews.run_stage(db.pipeline_settings(), say=lines.append,
                                   item_ids=[db.get_tech_by_uid(u)["id"] for u in ("hn:70", "geeknews:96", "geeknews:62")])
    log_text = "\n".join(lines)
    assert stats["tech_done"] == 2 and stats["tech_failed"] == 1, log_text
    texts = dict(written)
    assert "38.2% to 46.9%" in texts["hn:70"] and "side by side" in texts["geeknews:96"]
    assert db.get_tech_by_uid("geeknews:96")["url"] == "https://t3.codes/"
    assert db.get_tech_by_uid("geeknews:62")["status"] == "error"
    assert "read: 원문 (https://arxiv.org/abs/2610.18861)" in log_text and "Invalid character" not in log_text
    assert all("\n" not in line.strip() for line in lines)  # every log line is one line
    done = db.get_tech_by_uid("hn:70")["review"]
    assert done["_read"][0]["label"].startswith("원문") and done["_read"][0]["chars"] > 300


# --- collecting a period (a week, or whatever the settings say) ----------------------------------------------------

def test_github_trending_range_follows_the_period():
    asked = []

    def fake(url, params=None, **kw):
        asked.append(params["since"])
        return Resp(text=(FIX / "github_trending.html").read_text())

    with mock.patch.object(tech, "http_get", side_effect=fake):
        for days in (1, 7, 30):
            items = tech.github_trending(5, days)
    assert asked == ["daily", "weekly", "monthly"] and items[0]["meta"]["range"] == "monthly"
    assert [tech.trending_range(d) for d in (1, 2, 7, 8)] == ["daily", "weekly", "weekly", "monthly"]


def test_hacker_news_week_uses_the_period_best_stories():
    import time as _time

    now = int(_time.time())
    hits = [{"objectID": "1", "title": "Older but big", "url": "https://a.example", "points": 900, "num_comments": 300,
             "author": "x", "created_at_i": now - 5 * 86400},
            {"objectID": "2", "title": "Yesterday", "url": "https://b.example", "points": 400, "num_comments": 50,
             "author": "y", "created_at_i": now - 86400}]
    asked = {}

    def fake(url, params=None, **kw):
        if url == tech.HN_SEARCH:
            asked.update(params)
            return Resp(json={"hits": list(reversed(hits))})
        raise RuntimeError(url)

    with mock.patch.object(tech, "http_get", side_effect=fake):
        items = tech.hacker_news(10, days=7)
    since = int(asked["numericFilters"].split(",")[0].split(">")[1])
    assert abs(since - (now - 7 * 86400)) < 60 and "points>=120" in asked["numericFilters"]
    assert [i["uid"] for i in items] == ["hn:1", "hn:2"]  # best voted first
    assert items[0]["meta"] == {"points": 900, "comments": 300, "author": "x",
                                "discussion": "https://news.ycombinator.com/item?id=1", "time": now - 5 * 86400}


def test_hacker_news_week_falls_back_to_best_stories_within_the_period():
    import time as _time

    now = int(_time.time())
    stories = {1: {"type": "story", "title": "Fresh", "score": 300, "time": now - 2 * 86400, "url": "https://f.example"},
               2: {"type": "story", "title": "Too old", "score": 999, "time": now - 20 * 86400, "url": "https://o.example"},
               3: {"type": "story", "title": "Best of the week", "score": 700, "time": now - 6 * 86400, "url": "https://w.example"}}

    def fake(url, params=None, **kw):
        if url == tech.HN_SEARCH:
            raise RuntimeError("search down")
        if url.endswith("/topstories.json"):
            return Resp(json=[1, 2])
        if url.endswith("/beststories.json"):
            return Resp(json=[3, 1])
        return Resp(json=stories[int(url.rsplit("/", 1)[1].split(".")[0])])

    with mock.patch.object(tech, "http_get", side_effect=fake):
        items = tech.hacker_news(10, days=7)
    assert [i["title"] for i in items] == ["Best of the week", "Fresh"]


GN_PAST = """<html><body>
<div class="topic_row"><div class="topictitle"><a href="https://blog.example.com/week1" rel="nofollow"><h1>지난주 화제 1</h1></a>
  <span class="topicurl">(blog.example.com)</span></div>
  <div class="topicdesc"><a href="topic?id=501" class="c99 breakall">요약 하나</a></div>
  <div class="topicinfo"><span id="tp501">88</span> points by a 2일전 | <a href="topic?id=501&go=comments">댓글 3개</a></div></div>
<div class="topic_row"><div class="topictitle"><a href="https://github.com/acme/tool"><h1>지난주 화제 2</h1></a></div>
  <div class="topicdesc"><a href="topic?id=502">요약 둘</a></div>
  <div class="topicinfo"><span id="tp502">140</span> points</div></div>
</body></html>"""


def test_geeknews_list_page_is_parsed():
    items = tech.parse_geeknews_list(GN_PAST, "2026-10-15")
    assert [(i["uid"], i["title"], i["meta"]["points"]) for i in items] == [("geeknews:501", "지난주 화제 1", 88),
                                                                            ("geeknews:502", "지난주 화제 2", 140)]
    assert items[0]["meta"]["links"] == ["https://blog.example.com/week1"] and items[0]["summary"] == "요약 하나"
    assert items[0]["url"] == "https://news.hada.io/topic?id=501"


def test_geeknews_week_reads_each_day_and_ranks_by_points():
    days_asked = []

    def fake(url, params=None, **kw):
        if "rss" in url or "feedburner" in url:
            return Resp(text=(FIX / "geeknews.xml").read_text())
        if url.startswith(tech.GEEKNEWS_PAST):
            day = url.split("day=")[1]
            days_asked.append(day)
            return Resp(text=GN_PAST if len(days_asked) == 2 else "<html></html>")
        raise RuntimeError(url)

    with mock.patch.object(tech, "http_get", side_effect=fake), mock.patch.object(tech, "_published", return_value=None):
        items = tech.geeknews(10, days=7)
    assert len(days_asked) == 7
    assert [i["uid"] for i in items][:2] == ["geeknews:502", "geeknews:501"]  # best voted first
    assert "geeknews:23456" in [i["uid"] for i in items]  # the feed's topic is kept too


def test_geeknews_period_keeps_only_recent_feed_topics_when_the_site_is_closed():
    import requests

    def fake(url, params=None, **kw):
        if "rss" in url:
            return Resp(text=(FIX / "geeknews.xml").read_text())
        raise requests.HTTPError("403", response=mock.Mock(status_code=403, url=url))

    with mock.patch.object(tech, "http_get", side_effect=fake), mock.patch.object(tech, "render_page", return_value=None):
        week = tech.geeknews(10, days=7)
        with mock.patch.object(tech.time, "time", return_value=tech._published("2026-12-30T00:00:00+09:00")):
            later = tech.geeknews(10, days=7)
    assert [i["uid"] for i in week] == ["geeknews:23456"]  # the past lists are closed: the feed alone
    assert later == []  # published 2026-10-16: not within a week of 12-30


def test_hugging_face_period_leaves_out_untouched_items():
    import time as _time

    fresh = _time.strftime("%Y-%m-%dT%H:%M:%S.000Z", _time.gmtime(_time.time() - 3 * 86400))
    rows = [{"id": "acme/new", "likes": 50, "trendingScore": 90, "lastModified": fresh},
            {"id": "acme/stale", "likes": 900, "trendingScore": 80, "lastModified": "2024-01-01T00:00:00.000Z"}]

    def fake(url, params=None, **kw):
        return Resp(json=rows if url.endswith("/models") else [])

    with mock.patch.object(tech, "http_get", side_effect=fake):
        items = tech.hugging_face(10, days=7)
    assert [i["title"] for i in items] == ["acme/new"]


def test_collect_passes_the_period_to_every_source():
    got = {}
    fakes = {k: (lambda key: lambda limit, days: got.setdefault(key, (limit, days)) and [])(k) for k in tech.COLLECTORS}
    with mock.patch.dict(tech.COLLECTORS, fakes):
        tech.collect(list(tech.COLLECTORS), 25, say=lambda *a: None, days=7)
    assert got == {k: (25, 7) for k in tech.COLLECTORS}


def test_tech_period_settings_and_one_off_run(fresh_db):
    from paperagent import create_app, db, pipeline, technews

    app = create_app(start_scheduler=False)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    page = c.get("/settings").get_data(as_text=True)
    assert 'name="tech_lookback_days"' in page and 'name="tech_max_per_source"' in page
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    c.post("/settings/tech", data={"_csrf": token, "tech_per_run": "2", "tech_min_interest": "7", "tech_lookback_days": "7",
                                   "tech_max_per_source": "30", "src_hn": "1"})
    s = db.pipeline_settings()
    assert s["tech_lookback_days"] == 7 and s["tech_max_per_source"] == 30
    c.post("/settings/tech", data={"_csrf": token, "tech_per_run": "2", "tech_min_interest": "7", "tech_lookback_days": "99",
                                   "tech_max_per_source": "1", "src_hn": "1"})
    s = db.pipeline_settings()
    assert s["tech_lookback_days"] == 30 and s["tech_max_per_source"] == 5  # kept within bounds

    for url in ("/tech", "/queue?kind=tech"):
        html = c.get(url).get_data(as_text=True)
        assert 'name="days"' in html and "설정대로 · 30일" in html and ">일주일<" in html
    with mock.patch.object(pipeline, "run_in_background", return_value=True) as bg, \
            mock.patch("paperagent.agent.api_status", return_value={"ok": True}):
        c.post("/tech/run", data={"_csrf": token, "days": "7"})
        assert bg.call_args.kwargs["tech_days"] == 7
        c.post("/tech/run", data={"_csrf": token, "days": ""})
        assert bg.call_args.kwargs["tech_days"] is None

    # the run uses the one-off period instead of the setting
    seen = {}
    with mock.patch.object(technews, "run_stage", side_effect=lambda settings, *a, **k: seen.update(settings) or
                           {"tech_new": 0, "tech_done": 0, "tech_failed": 0, "tech_cost_usd": 0.0}), \
            mock.patch("paperagent.agent.ensure_api"):
        pipeline.run("manual", tech_only=True, tech_days=7)
    assert seen["tech_lookback_days"] == 7

    lines = []
    with mock.patch.object(tech, "collect", return_value=[]) as col:
        technews.collect_and_screen({**s, "tech_lookback_days": 7}, say=lines.append)
    assert col.call_args.kwargs["days"] == 7 and col.call_args.args[1] == 5 and "last 7 day(s)" in lines[0]
