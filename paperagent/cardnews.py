"""Instagram card-news generator: renders a 9-slide 1080x1350 (4:5) carousel to PNG.

The slides are laid out in HTML/CSS (templates/cards/cards.html) and captured
with headless Chromium through Playwright, so typography (Korean via the bundled
Pretendard font) stays crisp and the design is easy to tweak."""
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

from . import config, storage

log = logging.getLogger(__name__)
PKG_DIR = Path(__file__).resolve().parent
FONT_DIR = PKG_DIR / "static" / "fonts"

_env = Environment(loader=FileSystemLoader(PKG_DIR / "templates" / "cards"), autoescape=select_autoescape(["html"]))
_env.filters["nl2br"] = lambda s: Markup("<br>".join(escape(line) for line in str(s or "").replace("\\n", "\n").split("\n")))


def _text(s):
    """Card text: escaped, line breaks kept, and the * after an explained word raised."""
    html = _env.filters["nl2br"](s)
    return Markup(re.sub(r"(?<=\S)\*", '<sup class="star">*</sup>', str(html)))


_env.filters["t"] = _text


def _source_ref(paper):
    if paper["uid"].startswith("arxiv:"):
        return f"arXiv:{paper['uid'][6:]}"
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


def build_html(paper: dict, review: dict, figures, page1=None) -> str:
    """figures: [{number, caption, path}] with local file paths."""
    card = normalize_card(review)
    figs = {f["number"]: {**f, "uri": Path(f["path"]).resolve().as_uri()} for f in figures}

    def fig(part):
        return figs.get((card.get(part) or {}).get("figure_number") or 0)

    # One entry per slide, in order; a slide whose content is missing is left out.
    slides = [{"kind": "cover"}]
    if card.get("problem", {}).get("points"):
        slides.append({"kind": "problem", "label": "문제", **card["problem"], "figure": fig("problem")})
    if card.get("idea", {}).get("body"):
        slides.append({"kind": "idea", "label": "핵심 아이디어", **card["idea"], "figure": fig("idea")})
    if card.get("steps"):
        slides.append({"kind": "steps", "label": "작동 방식", "steps": card["steps"]})
    if card.get("setup"):
        slides.append({"kind": "setup", "label": "실험 설정", "rows": card["setup"]})
    if card.get("results"):
        slides.append({"kind": "results", "label": "결과", **card["results"], "figure": fig("results")})
    slides.append({"kind": "verdict", "label": "평가", **card["verdict"]})
    slides.append({"kind": "summary"})
    slides.append({"kind": "outro"})

    explained = set()
    for s in slides:
        text = json.dumps({k: v for k, v in s.items() if k != "figure"}, ensure_ascii=False)
        if s["kind"] == "cover":
            text += json.dumps([card.get(k) for k in ("cover_headline", "cover_sub", "cover_points")], ensure_ascii=False)
        elif s["kind"] == "summary":
            text += json.dumps(card.get("summary_rows"), ensure_ascii=False)
        s["notes"] = [t for t in card.get("terms") or [] if t.get("term") and f"{t['term']}*" in text and t["term"] not in explained]
        explained.update(t["term"] for t in s["notes"])

    # Cover visual: a figure not shown on later slides, else the paper's
    # first page, else any figure.
    used = {s["figure"]["number"] for s in slides if s.get("figure")}
    cover = next((f for n, f in figs.items() if n not in used), None)
    cover_uri = cover["uri"] if cover else None
    if not cover_uri and page1 and Path(page1).exists():
        cover_uri = Path(page1).resolve().as_uri()
    if not cover_uri and figs:
        cover_uri = next(iter(figs.values()))["uri"]

    return _env.get_template("cards.html").render(
        fonts=FONT_DIR.resolve().as_uri(),
        brand=config.CARD_BRAND,
        handle=config.CARD_HANDLE,
        acronym=config.BRAND_ACRONYM,
        paper=paper,
        review=review,
        card=card,
        slides=slides,
        total=len(slides),
        cover_image=cover_uri,
        source_ref=_source_ref(paper),
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


def render(paper: dict, review: dict, figures, out_dir: Path, page1=None):
    """Render all slides to out_dir/card_XX.png; returns the list of paths."""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("card_*.png"):
        old.unlink()
    html_path = out_dir / "cards.html"
    html_path.write_text(build_html(paper, review, figures, page1), encoding="utf-8")
    paths = []
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={"width": 1080, "height": 1350}, device_scale_factor=1)
            page.goto(html_path.as_uri())
            page.wait_for_selector("body[data-ready='1']", timeout=30000)
            for i, el in enumerate(page.query_selector_all("section.slide"), start=1):
                path = out_dir / f"card_{i:02d}.png"
                el.screenshot(path=str(path))
                paths.append(str(path))
        finally:
            browser.close()
    (out_dir / "caption.txt").write_text(build_caption(paper, review), encoding="utf-8")
    return paths


_HASHTAG = re.compile(r"#[^\s#]+")


def hashtags(card: dict) -> list[str]:
    """The fixed account tags plus the paper's own, without duplicates, capped at
    Instagram's per-post limit. Older reviews carried their tags in the caption."""
    own = card.get("hashtags") or _HASHTAG.findall(card.get("caption") or "")
    out = []
    for tag in config.CARD_BASE_HASHTAGS + own:
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


def render_and_store(paper: dict, review: dict) -> list[str]:
    """Render the cards for a reviewed paper and save them to storage.

    Figures come from storage (keys in review["_figures"]); the HTML and PNGs
    are built in a temporary directory. Returns the storage keys of the cards."""
    pid = paper["id"]
    with tempfile.TemporaryDirectory(prefix="cards-") as tmp:
        tmp = Path(tmp)
        figures = []
        for f in review.get("_figures", []):
            local = storage.materialize(f["key"], tmp)
            if local:
                figures.append({**f, "path": str(local)})
        page1 = storage.materialize(review["_page1"], tmp) if review.get("_page1") else None
        paths = render(paper, review, figures, tmp / "out", page1)
        prefix = f"cards/{pid}/"
        storage.delete_prefix(prefix)
        keys = [storage.put_file(prefix + Path(p).name, p) for p in paths]
        storage.put(prefix + "caption.txt", (tmp / "out" / "caption.txt").read_bytes(), "text/plain; charset=utf-8")
    return keys


def zip_cards(paper_id: int, card_keys) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key in card_keys:
            item = storage.get(key)
            if item:
                zf.writestr(Path(key).name, item[0])
        caption = storage.get(f"cards/{paper_id}/caption.txt")
        if caption:
            zf.writestr("caption.txt", caption[0])
    return buf.getvalue()


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "paper"
