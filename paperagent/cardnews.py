"""Instagram card-news generator: renders a 6-slide 1080x1350 (4:5) carousel to PNG.

The slides are laid out in HTML/CSS (templates/cards/cards.html) and captured
with headless Chromium through Playwright, so typography (Korean via the bundled
Pretendard font) stays crisp and the design is easy to tweak."""
import logging
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from . import config

log = logging.getLogger(__name__)
PKG_DIR = Path(__file__).resolve().parent
FONT_DIR = PKG_DIR / "static" / "fonts"

_env = Environment(loader=FileSystemLoader(PKG_DIR / "templates" / "cards"), autoescape=select_autoescape(["html"]))
_env.filters["nl2br"] = lambda s: Markup("<br>".join(escape(line) for line in str(s or "").replace("\\n", "\n").split("\n")))


def _source_ref(paper):
    if paper["uid"].startswith("arxiv:"):
        return f"arXiv:{paper['uid'][6:]}"
    if paper.get("doi"):
        return f"doi:{paper['doi']}"
    return paper.get("source_name") or "source"


def _authors_short(authors):
    if not authors:
        return ""
    return authors[0] + (" 외" if len(authors) > 1 else "")


def build_html(paper: dict, review: dict, figures) -> str:
    card = review["card"]
    figs = {f["number"]: {**f, "uri": Path(f["path"]).resolve().as_uri()} for f in figures}
    slides = []
    for s in card["slides"][:3]:
        slides.append({**s, "figure": figs.get(s.get("figure_number") or 0)})
    used = {s["figure"]["number"] for s in slides if s["figure"]}

    # Cover visual: a figure not shown on later slides, else the paper's
    # first page, else any figure.
    cover = next((f for n, f in figs.items() if n not in used), None)
    cover_uri = cover["uri"] if cover else None
    if not cover_uri and paper.get("_page1") and Path(paper["_page1"]).exists():
        cover_uri = Path(paper["_page1"]).resolve().as_uri()
    if not cover_uri and figs:
        cover_uri = next(iter(figs.values()))["uri"]

    published = (paper.get("published") or "")[:10]
    try:
        d = datetime.fromisoformat(published)
        date_label = f"{d.year}년 {d.month}월"
    except ValueError:
        date_label = published
    title = paper["title"]
    return _env.get_template("cards.html").render(
        fonts=FONT_DIR.resolve().as_uri(),
        brand=config.CARD_BRAND,
        handle=config.CARD_HANDLE,
        acronym=config.BRAND_ACRONYM,
        issue_no=f"{paper.get('id') or 0:04d}",
        issue_date=datetime.now().strftime("%Y.%m.%d"),
        paper=paper,
        review=review,
        card=card,
        slides=slides,
        total=len(slides) + 3,
        cover_image=cover_uri,
        source_ref=_source_ref(paper),
        source_label=paper.get("source_name") or ("arXiv" if paper["source_kind"] == "arxiv" else ""),
        authors_short=_authors_short(paper.get("authors")),
        paper_short=(title[:38] + "…") if len(title) > 40 else title,
        date_label=date_label,
    )


def _launch(p):
    kwargs = {"args": ["--no-sandbox", "--font-render-hinting=none"]}
    candidates = [config.CHROMIUM_EXECUTABLE] if config.CHROMIUM_EXECUTABLE else []
    candidates += [None, "/opt/pw-browsers/chromium", "/usr/bin/chromium", "/usr/bin/chromium-browser"]
    last = None
    for exe in candidates:
        if exe and not os.path.exists(exe):
            continue
        try:
            return p.chromium.launch(executable_path=exe, **kwargs) if exe else p.chromium.launch(**kwargs)
        except Exception as exc:  # try the next candidate
            last = exc
    raise RuntimeError(f"Could not launch Chromium for card rendering: {last}")


def render(paper: dict, review: dict, figures, out_dir: Path):
    """Render all slides to out_dir/card_XX.png; returns the list of paths."""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("card_*.png"):
        old.unlink()
    html_path = out_dir / "cards.html"
    html_path.write_text(build_html(paper, review, figures), encoding="utf-8")
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


def build_caption(paper, review):
    caption = review["card"]["caption"].replace("\\n", "\n").strip()
    link = paper.get("url") or ""
    return f"{caption}\n\n📄 원문: {paper['title']}\n🔗 {link}\n⭐ 평점 {review['overall_score']}/10 (방법론 {review['methodology_score']} · 참신성 {review['novelty_score']})"


def zip_cards(card_paths, caption_path, zip_path: Path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in card_paths:
            zf.write(p, Path(p).name)
        if caption_path and Path(caption_path).exists():
            zf.write(caption_path, "caption.txt")
    return zip_path


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "paper"
