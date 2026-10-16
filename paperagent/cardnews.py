"""Card news: 1080x1350 (4:5) carousels rendered to PNG; the closing slide also as MP4.

Two categories share one frame and differ by colour and label, so a scroll
through the feed shows at a glance what kind of post it is:

  paper - a reviewed paper (indigo)    cards/<id>/card_NN.png
  tech  - a trending project or news   tech/<id>/cards/card_NN.png
          (terracotta; GitHub Trending, Hacker News, GeekNews, Hugging Face)

The slides are laid out in HTML/CSS (templates/cards/cards.html) and captured
with headless Chromium through Playwright, so Korean typography (the bundled
Pretendard font) stays crisp. Every slide is a still image except the closing
one, which is also written as a short MP4 with the same animation on every post:
its words rise into place one after another (video.py encodes the frames).
"""
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from . import config, storage, video

log = logging.getLogger(__name__)
PKG_DIR = Path(__file__).resolve().parent
FONT_DIR = PKG_DIR / "static" / "fonts"

# White slides; a light tint of the category colour for the header and panels,
# the category colour itself only for small accents. Every text pair meets
# WCAG AA contrast.
THEMES = {
    "paper": {"key": "paper", "color": "#3A5A9F", "deep": "#1E2A47", "soft": "#EEF2F9", "tint": "#AEBFE8", "label": "PAPER",
              "name": "논문 리뷰", "claim": "읽을 가치가 있는 논문만 골라 읽고,\n평가합니다."},
    "tech": {"key": "tech", "color": "#A84B24", "deep": "#2A221D", "soft": "#FAF0E9", "tint": "#F0A983", "label": "TECH",
             "name": "테크 트렌드", "claim": "지금 개발자들이 주목하는 기술을 골라,\n쉽게 풀어 드립니다."},
}

TECH_BASE_HASHTAGS = config.CARD_TECH_BASE_HASHTAGS

_env = Environment(loader=FileSystemLoader(PKG_DIR / "templates" / "cards"), autoescape=select_autoescape(["html"]))


_env.filters["nl2br"] = lambda s: Markup("<br>".join(escape(line) for line in str(s or "").replace("\\n", "\n").split("\n")))


def _text(s):
    """Card text: escaped, line breaks kept, and the * after an explained word raised."""
    html = _env.filters["nl2br"](s)
    return Markup(re.sub(r"(?<=\S)\*", '<sup class="star">*</sup>', str(html)))


_env.filters["t"] = _text


def _source_ref(paper):
    uid = paper.get("uid") or ""
    if uid.startswith("arxiv:"):
        return f"arXiv:{uid[6:]}"
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([\w.\-]+?)(?:v\d+)?$", paper.get("url") or "")
    if m:
        return f"arXiv:{m.group(1)}"
    if paper.get("doi"):
        return f"doi:{paper['doi']}"
    return paper.get("source_name") or "source"


def _lines(text):
    return [x.strip() for x in str(text or "").replace("\\n", "\n").split("\n") if x.strip()]


def normalize_card(review: dict) -> dict:
    """The card copy in the current shape. Reviews written before the current
    layout (card["slides"]) are mapped onto it as well as they fit."""
    card = dict(review.get("card") or {})
    old = card.get("slides")
    if old and "problem" not in card:
        old = old + [{}] * (3 - len(old))
        p, m, r = old[:3]
        card["problem"] = {"headline": p.get("headline", ""), "points": _lines(p.get("body")), "figure_number": p.get("figure_number", 0)}
        card["idea"] = {"headline": m.get("headline", ""), "body": " ".join(_lines(m.get("body"))), "figure_number": m.get("figure_number", 0)}
        card["results"] = {"headline": r.get("headline", ""), "highlight": r.get("highlight", ""),
                           "highlight_label": r.get("highlight_label", ""), "figure_number": r.get("figure_number", 0),
                           "rows": [{"item": x, "value": "", "note": ""} for x in review.get("key_results", [])[:4]]}
    card.setdefault("cover_points", review.get("key_points", [])[:3])
    card.setdefault("steps", [{"title": s.get("title", ""), "detail": s.get("detail", "")} for s in review.get("method_steps", [])[:4]])
    card.setdefault("setup", [r for r in review.get("setup", [])[:4]])
    card.setdefault("verdict", {"strengths": review.get("strengths", [])[:2], "weaknesses": review.get("weaknesses", [])[:2],
                                "who": review.get("who_should_read", "")})
    return card


def _bars(rows):
    """Bars for the results slide: only real numbers, scaled to the largest."""
    out = []
    for b in rows or []:
        try:
            value = float(b.get("value"))
        except (TypeError, ValueError):
            continue
        if value >= 0 and b.get("label"):
            out.append({**b, "value": value})
    if len(out) < 2:
        return []
    top = max(b["value"] for b in out) or 1
    for b in out:
        b["pct"] = max(4, round(b["value"] / top * 100))
        b["display"] = b.get("display") or f"{b['value']:g}"
    return out[:4]


def _usable_figures(figures):
    """Figures worth showing on a card: big enough to read, not a thin strip."""
    out = {}
    for f in figures:
        size = _image_size(f["path"])
        if size:
            w, h = size
            if w < 360 or h < 160 or w / h > 3.6 or h / w > 2.2:
                continue
            f = {**f, "w": w, "h": h}
        out[f["number"]] = {**f, "uri": Path(f["path"]).resolve().as_uri()}
    return out


def _image_size(path):
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def _attach_notes(slides, card, extra_text=None):
    """Each explained word (marked with *) gets its meaning at the bottom of the
    first slide it appears on."""
    explained = set()
    for s in slides:
        text = json.dumps({k: v for k, v in s.items() if k not in ("figure", "image")}, ensure_ascii=False)
        text += json.dumps((extra_text or {}).get(s["kind"]), ensure_ascii=False)
        s["notes"] = [t for t in card.get("terms") or [] if t.get("term") and f"{t['term']}*" in text and t["term"] not in explained]
        explained.update(t["term"] for t in s["notes"])


def paper_slides(paper, review, figures):
    """The slides of a paper's carousel, in order; a slide with nothing to say is left out."""
    card = normalize_card(review)
    figs = _usable_figures(figures)

    def fig(part):
        return figs.get((card.get(part) or {}).get("figure_number") or 0)

    slides = [{"kind": "cover"}]
    problem = card.get("problem") or {}
    if problem.get("points"):
        slides.append({"kind": "points", "label": "문제", "headline": problem.get("headline"), "points": problem["points"],
                       "note_title": "배경", "note": problem.get("context") or review.get("background"), "figure": fig("problem")})
    idea = card.get("idea") or {}
    if idea.get("body"):
        slides.append({"kind": "media", "label": "핵심 아이디어", "headline": idea.get("headline"), "body": idea["body"],
                       "figure": fig("idea")})
    if card.get("steps"):
        slides.append({"kind": "steps", "label": "작동 방식", "headline": "어떻게 작동하나요", "steps": card["steps"][:4]})
    if card.get("setup"):
        slides.append({"kind": "rows", "label": "실험 설정", "headline": "어떻게 실험했나요", "rows": card["setup"][:5],
                       "note_title": "분석에서 확인한 것", "note_list": (card.get("findings") or review.get("analysis") or [])[:2]})
    res = card.get("results") or {}
    if res:
        slides.append({"kind": "numbers", "label": "결과", "headline": res.get("headline"), "highlight": res.get("highlight"),
                       "highlight_label": res.get("highlight_label"), "bars": _bars(res.get("bars")),
                       "rows": res.get("rows") or [], "figure": fig("results")})
    so = card.get("so_what") or {}
    if so.get("items"):
        slides.append({"kind": "sowhat", "label": "그래서 나랑 무슨 상관?", "headline": so.get("headline"), "items": so["items"][:3]})
    slides.append({"kind": "verdict", "label": "평가", "headline": "얼마나 믿을 만한가요", **card["verdict"]})
    slides.append({"kind": "summary", "headline": "한눈에 정리", "rows": card.get("summary_rows") or []})
    slides.append({"kind": "outro"})

    # The key-idea slide always gets a picture: its own figure, else the first unused one.
    used = {s["figure"]["number"] for s in slides if s.get("figure")}
    for s in slides:
        if s["kind"] == "media" and not s.get("figure"):
            spare = next((f for n, f in figs.items() if n not in used), None)
            if spare:
                s["figure"] = spare
                used.add(spare["number"])

    cover_image = next((f for n, f in figs.items() if n not in used), None) or (
        next((s["figure"] for s in slides if s["kind"] == "media" and s.get("figure")), None))
    cover = {
        "headline": card.get("cover_headline"), "sub": card.get("cover_sub"), "topic": card.get("category"),
        "image": cover_image, "stat": {"type": "score", "overall": review.get("overall_score"),
                                       "method": review.get("methodology_score"), "novelty": review.get("novelty_score")},
    }
    _attach_notes(slides, card, {"cover": [card.get(k) for k in ("cover_headline", "cover_sub")]})
    return slides, cover


def build_html(paper: dict, review: dict, figures, page1=None, kind="paper") -> str:
    """figures: [{number, caption, path}] with local file paths. (page1 is
    accepted for older callers; the first page is no longer shown - a page of
    body text makes a poor picture.)"""
    slides, cover = paper_slides(paper, review, figures)
    return render_html(THEMES["paper"], slides, cover, source_ref=_source_ref(paper), review=review)


def render_html(theme, slides, cover, source_ref, review=None) -> str:
    return _env.get_template("cards.html").render(
        fonts=FONT_DIR.resolve().as_uri(),
        brand=config.CARD_BRAND,
        handle=config.CARD_HANDLE,
        acronym=config.BRAND_ACRONYM,
        theme=theme,
        review=review or {},
        cover=cover,
        slides=slides,
        total=len(slides),
        source_ref=source_ref,
    )


def _launch(p):
    kwargs = {"args": ["--no-sandbox", "--font-render-hinting=none", "--disable-dev-shm-usage"]}
    candidates = [config.CHROMIUM_EXECUTABLE] if config.CHROMIUM_EXECUTABLE else []
    # None = Playwright's own download; then Chrome from the Heroku
    # chrome-for-testing buildpack and common system installs.
    candidates += [None, shutil.which("chrome"), "/app/.chrome-for-testing/chrome-linux64/chrome",
                   shutil.which("google-chrome"), shutil.which("chromium"), "/opt/pw-browsers/chromium",
                   "/usr/bin/chromium", "/usr/bin/chromium-browser"]
    last = None
    for exe in candidates:
        if exe and not os.path.exists(exe):
            continue
        try:
            return p.chromium.launch(executable_path=exe, **kwargs) if exe else p.chromium.launch(**kwargs)
        except Exception as exc:  # try the next candidate
            last = exc
    raise RuntimeError(f"Could not launch Chromium for card rendering: {last}")


def capture(html: str, out_dir: Path, videos=True):
    """Screenshot every slide of the HTML to out_dir/card_NN.png and write the
    video slides as card_NN.mp4. Returns (png paths, {slide number: mp4 path})."""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in list(out_dir.glob("card_*.png")) + list(out_dir.glob("card_*.mp4")):
        old.unlink()
    html_path = out_dir / "cards.html"
    html_path.write_text(html, encoding="utf-8")
    paths, clips = [], {}
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={"width": 1080, "height": 1350}, device_scale_factor=1)
            page.goto(html_path.as_uri())
            page.wait_for_selector("body[data-ready='1']", timeout=30000)
            slides = page.query_selector_all("section.slide")
            for i, el in enumerate(slides, start=1):
                path = out_dir / f"card_{i:02d}.png"
                el.screenshot(path=str(path))
                paths.append(str(path))
            last = slides[-1] if slides else None
            if videos and last is not None and last.get_attribute("data-video") == "outro" and video.available():
                try:
                    clips[len(slides)] = str(_outro_video(page, last, len(slides), out_dir))
                except Exception as exc:  # the still card is still there
                    log.warning("closing video failed: %s", exc)
        finally:
            browser.close()
    return paths, clips


def _outro_video(page, el, n, out_dir: Path):
    """The closing slide as an MP4: start its CSS animations (.play), step them
    frame by frame with the Web Animations API, screenshot each frame and encode."""
    frames = out_dir / f"frames_{n:02d}"
    shutil.rmtree(frames, ignore_errors=True)
    frames.mkdir()
    el.scroll_into_view_if_needed()
    box = el.bounding_box()
    el.evaluate("(s) => { s.classList.add('play'); s.getAnimations({ subtree: true }).forEach((a) => a.pause()); }")
    try:
        for i in range(round(video.OUTRO_SECONDS * video.FPS)):
            el.evaluate("(s, t) => s.getAnimations({ subtree: true }).forEach((a) => { a.currentTime = t; })", i * 1000 / video.FPS)
            page.screenshot(path=str(frames / f"{i:04d}.jpg"), type="jpeg", quality=94, clip=box)
    finally:
        el.evaluate("(s) => s.classList.remove('play')")
    out = video.encode(frames, out_dir / f"card_{n:02d}.mp4")
    shutil.rmtree(frames, ignore_errors=True)
    return out


def render(paper: dict, review: dict, figures, out_dir: Path, page1=None, videos=False):
    """Render a paper's slides to out_dir/card_XX.png; returns the list of paths.
    With videos=True the closing slide is also written as card_XX.mp4."""
    paths, _ = capture(build_html(paper, review, figures), out_dir, videos=videos)
    (out_dir / "caption.txt").write_text(build_caption(paper, review), encoding="utf-8")
    return paths


_HASHTAG = re.compile(r"#[^\s#]+")


def hashtags(card: dict, base=None) -> list[str]:
    """The fixed account tags plus the post's own, without duplicates, capped at
    Instagram's per-post limit. Older reviews carried their tags in the caption."""
    own = card.get("hashtags") or _HASHTAG.findall(card.get("caption") or "")
    out = []
    for tag in (config.CARD_BASE_HASHTAGS if base is None else base) + own:
        tag = "#" + tag.strip().lstrip("#").replace(" ", "")
        if len(tag) > 1 and tag.lower() not in {t.lower() for t in out}:
            out.append(tag)
    return out[: config.CARD_HASHTAG_LIMIT]


def build_caption(paper, review):
    card = review["card"]
    body = _HASHTAG.sub("", card["caption"].replace("\\n", "\n")).strip()
    link = paper.get("url") or ""
    return (f"{body}\n\n📄 원문: {paper['title']}\n🔗 {link}\n"
            f"⭐ 평점 {review['overall_score']}/10 (방법론 {review['methodology_score']} · 참신성 {review['novelty_score']})\n\n"
            + " ".join(hashtags(card)))


def store(prefix: str, paths, clips, caption: str):
    """Replace what is stored under prefix with the new cards. Returns
    (png keys, {slide number as str: mp4 key})."""
    storage.delete_prefix(prefix)
    keys = [storage.put_file(prefix + Path(p).name, p) for p in paths]
    videos = {str(n): storage.put_file(prefix + Path(p).name, p) for n, p in clips.items()}
    storage.put(prefix + "caption.txt", caption.encode("utf-8"), "text/plain; charset=utf-8")
    return keys, videos


def render_and_store(paper: dict, review: dict):
    """Render the cards for a reviewed paper and save them to storage.

    Figures come from storage (keys in review["_figures"]); the HTML, PNGs and
    the MP4 are built in a temporary directory. Returns (card keys, video keys)."""
    pid = paper["id"]
    with tempfile.TemporaryDirectory(prefix="cards-") as tmp:
        tmp = Path(tmp)
        figures = []
        for f in review.get("_figures", []):
            local = storage.materialize(f["key"], tmp)
            if local:
                figures.append({**f, "path": str(local)})
        paths, clips = capture(build_html(paper, review, figures), tmp / "out")
        return store(f"cards/{pid}/", paths, clips, build_caption(paper, review))


# --- TECH posts ------------------------------------------------------------------

def tech_tiles(item) -> list:
    """The source's own numbers, for the cover and the numbers slide (never AI-written)."""
    from .collectors.tech import compact

    m = item.get("meta") or {}
    src = item["source"]
    tiles = []
    if src == "github":
        if m.get("stars") is not None:
            tiles.append({"value": compact(m["stars"]), "label": "GitHub 스타"})
        if m.get("stars_today"):
            tiles.append({"value": "+" + compact(m["stars_today"]), "label": "오늘 받은 스타"})
        elif m.get("new_this_week"):
            tiles.append({"value": "NEW", "label": "이번 주 공개"})
        if m.get("forks"):
            tiles.append({"value": compact(m["forks"]), "label": "포크(복사해 간 수)"})
        if m.get("language"):
            tiles.append({"value": m["language"], "label": "주 언어"})
    elif src == "hn":
        if m.get("points") is not None:
            tiles.append({"value": compact(m["points"]), "label": "Hacker News 추천"})
        if m.get("comments") is not None:
            tiles.append({"value": compact(m["comments"]), "label": "댓글"})
    elif src == "hf":
        if m.get("likes") is not None:
            tiles.append({"value": compact(m["likes"]), "label": "좋아요"})
        if m.get("downloads"):
            tiles.append({"value": compact(m["downloads"]), "label": "최근 다운로드"})
        if m.get("task") or m.get("sdk"):
            tiles.append({"value": m.get("task") or m.get("sdk"), "label": "분야" if m.get("task") else "형태"})
    return tiles


def _tech_ref(item) -> str:
    from urllib.parse import urlparse

    if item["source"] == "github":
        return f"github.com/{item['title']}"
    if item["source"] == "hn":
        return f"news.ycombinator.com · {item['uid'][3:]}"
    if item["source"] == "geeknews":
        return f"news.hada.io · {item['uid'].split(':')[1]}"
    url = urlparse(item.get("url") or "")
    return (url.netloc + url.path).rstrip("/")[:48] or item["source"]


def _poster(clip: str, out: Path):
    """A still from the middle of a demo clip (its first frame is often still empty)."""
    import subprocess

    at = min((video.media_seconds(clip) or 0) * 0.6, 8.0)
    subprocess.run([video.ffmpeg_exe(), "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", clip, "-frames:v", "1", str(out)],
                   check=True, timeout=60)
    return out if out.exists() else None


def tech_slides(item, review, media, tmp: Path):
    """Slides for a TECH post. media: {"images": [local paths], "video": local path|None}."""
    from .collectors.tech import SOURCES

    def photo(path, caption=""):
        return {"uri": Path(path).resolve().as_uri(), "path": str(path), "photo": True, "caption": caption, "number": 0} if path else None

    images = list(media.get("images") or [])
    clip = media.get("video")
    poster = None
    if clip and video.ffmpeg_exe():
        try:
            poster = _poster(clip, tmp / "poster.png")
        except Exception as exc:
            log.warning("demo still failed: %s", exc)
    tiles = tech_tiles(item)
    src_name = SOURCES.get(item["source"], {}).get("short", item["source"])

    slides = [{"kind": "cover"}]
    what = review.get("what") or {}
    if what.get("points"):
        slides.append({"kind": "points", "label": "무엇인가요", "headline": what.get("headline"), "points": what["points"],
                       "note_title": "왜 지금 화제일까", "note": what.get("why_now")})
    demo = review.get("demo") or {}
    show = photo(poster, "") if poster else photo(images[1] if len(images) > 1 else (images[0] if images else None))
    if show:
        slides.append({"kind": "media", "label": "직접 보기", "headline": demo.get("headline") or review.get("title_ko"),
                       "body": demo.get("body"), "figure": show})
    if review.get("how"):
        slides.append({"kind": "steps", "label": "어떻게 작동하나요", "headline": "핵심만 짚어 보면", "steps": review["how"][:4]})
    if tiles:
        slides.append({"kind": "numbers", "label": "숫자로 보기", "headline": review.get("numbers_headline") or "얼마나 주목받고 있나요",
                       "highlight": tiles[0]["value"], "highlight_label": tiles[0]["label"], "tiles": tiles[1:5]})
    so = review.get("so_what") or {}
    if so.get("items"):
        slides.append({"kind": "sowhat", "label": "그래서 나랑 무슨 상관?", "headline": so.get("headline"), "items": so["items"][:3]})
    try_it = review.get("try_it") or {}
    if try_it.get("steps"):
        slides.append({"kind": "steps", "label": "직접 써보기", "headline": try_it.get("headline") or "이렇게 시작해 보세요",
                       "steps": try_it["steps"][:3], "link": item.get("url")})
    slides.append({"kind": "summary", "headline": "한눈에 정리", "rows": review.get("summary_rows") or []})
    slides.append({"kind": "outro"})

    cover = {"headline": review.get("cover_headline"), "sub": review.get("cover_sub"),
             "topic": f"{src_name} · {review.get('category')}" if review.get("category") else src_name,
             "image": photo(images[0]) if images else None, "stat": {"tiles": tiles[:3]} if tiles else {}}
    _attach_notes(slides, review, {"cover": [review.get("cover_headline"), review.get("cover_sub")]})
    return slides, cover


def build_tech_html(item, review, media, tmp: Path) -> str:
    slides, cover = tech_slides(item, review, media, tmp)
    return render_html(THEMES["tech"], slides, cover, source_ref=_tech_ref(item), review=review)


def render_tech_and_store(item: dict, review: dict):
    """Render a TECH post and save it under tech/<id>/. Returns (card keys, video keys)."""
    from . import technews

    with tempfile.TemporaryDirectory(prefix="tech-") as tmp:
        tmp = Path(tmp)
        media = technews.materialize_media(item, tmp)
        paths, clips = capture(build_tech_html(item, review, media, tmp), tmp / "out")
        prefix = f"tech/{item['id']}/cards/"
        return store(prefix, paths, clips, technews.build_caption(item, review))


def zip_cards(prefix: str, card_keys, video_keys=None) -> bytes:
    """All slides in posting order (a video slide as its .mp4) and the caption."""
    video_keys = video_keys or {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, key in enumerate(card_keys, start=1):
            key = video_keys.get(str(n)) or key
            item = storage.get(key)
            if item:
                zf.writestr(Path(key).name, item[0])
        caption = storage.get(prefix + "caption.txt")
        if caption:
            zf.writestr("caption.txt", caption[0])
    return buf.getvalue()


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "paper"
