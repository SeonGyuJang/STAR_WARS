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
    db.update_tech(tid, status="done", review={"title_ko": "글", "one_liner": "한 줄", "summary": "요약", "caption": "c"})
    app = create_app(start_scheduler=False)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    page = c.get(f"/tech/{tid}").get_data(as_text=True)
    assert "다시 읽기" in page and f"/tech/{tid}/reprocess" in page
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    with mock.patch.object(pipeline, "run_in_background", return_value=True) as bg, \
            mock.patch("paperagent.agent.api_status", return_value={"ok": True}):
        r = c.post(f"/tech/{tid}/reprocess", data={"_csrf": token})
    assert r.status_code == 302 and bg.call_args.kwargs == {"tech_only": True, "tech_ids": [tid]}
    visitor = app.test_client()
    visitor.environ_base["REMOTE_ADDR"] = "10.0.0.9"
    assert "다시 읽기" not in visitor.get(f"/tech/{tid}").get_data(as_text=True)
