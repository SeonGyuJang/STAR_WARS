"""PDF download (saved under the paper title) and figure extraction."""
import logging
import re
from pathlib import Path

import pymupdf as fitz

from . import config
from .collectors import http_get

log = logging.getLogger(__name__)

CAPTION_RE = re.compile(r"^\s*(Figure|Fig\.?)\s*(\d+)\s*[:.|]", re.IGNORECASE)
MAX_PDF_BYTES = 60 * 1024 * 1024


def safe_filename(title: str, max_len: int = 150) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return (name[:max_len].rstrip() or "untitled") + ".pdf"


def download_pdf(paper: dict) -> Path:
    """Download the paper PDF to data/pdfs/<Paper Title>.pdf."""
    if not paper.get("pdf_url"):
        raise ValueError("no open-access PDF url")
    target = config.PDF_DIR / safe_filename(paper["title"])
    if target.exists() and target.stat().st_size > 0:
        # Same title from another paper: keep both by suffixing the uid.
        if paper.get("local_pdf") != str(target):
            suffix = re.sub(r"[^A-Za-z0-9.]+", "_", paper["uid"])
            target = target.with_name(f"{target.stem} [{suffix}].pdf")
    resp = http_get(paper["pdf_url"], timeout=120, stream=True)
    data = b""
    for chunk in resp.iter_content(1 << 16):
        data += chunk
        if len(data) > MAX_PDF_BYTES:
            raise ValueError("PDF larger than 60MB")
    if not data.startswith(b"%PDF"):
        raise ValueError(f"not a PDF (content-type {resp.headers.get('content-type')})")
    target.write_bytes(data)
    return target


def pdf_text(path: Path, max_chars: int = 400_000) -> str:
    with fitz.open(path) as doc:
        text = "\n".join(page.get_text() for page in doc)
    return text[:max_chars]


def page_count(path: Path) -> int:
    with fitz.open(path) as doc:
        return doc.page_count


def _graphic_rects(page):
    """Bounding boxes of raster images and vector drawings on the page."""
    page_area = page.rect.width * page.rect.height
    rects = []
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"])
        if r.width > 30 and r.height > 30:
            rects.append(r)
    try:
        drawings = page.get_drawings()
    except Exception:  # malformed content streams
        drawings = []
    for d in drawings:
        r = fitz.Rect(d["rect"])
        if r.width < 2 and r.height < 2:
            continue
        if r.width * r.height > 0.9 * page_area:  # page frames/backgrounds
            continue
        rects.append(r)
    return rects


def _union(rects):
    u = fitz.Rect(rects[0])
    for r in rects[1:]:
        u |= r
    return u


def extract_figures(pdf_path: Path, out_dir: Path, max_figures: int = 12, zoom: float = 2.5):
    """Crop 'Figure N' regions: the graphics stacked directly above each caption.

    Returns [{number, caption, page, path}] sorted by figure number."""
    out_dir.mkdir(parents=True, exist_ok=True)
    figures = {}
    with fitz.open(pdf_path) as doc:
        for page in doc:
            blocks = [b for b in page.get_text("blocks") if b[6] == 0]  # text blocks only
            captions = [b for b in blocks if CAPTION_RE.match(b[4])]
            if not captions:
                continue
            graphics = _graphic_rects(page)
            page_rect = page.rect
            for cap in captions:
                num = int(CAPTION_RE.match(cap[4]).group(2))
                if num in figures:
                    continue  # first occurrence wins (later ones are references)
                cap_rect = fitz.Rect(cap[:4])
                # Upper bound: the closest paragraph of body text above the caption
                # (wide, multi-line, not overlapping any graphic).
                top_limit = page_rect.y0 + 20
                for b in blocks:
                    r = fitz.Rect(b[:4])
                    if r.y1 > cap_rect.y0 - 2 or b is cap:
                        continue
                    is_paragraph = r.width > 0.35 * page_rect.width and b[4].count("\n") >= 3
                    overlaps_graphic = any(r.intersects(g) for g in graphics)
                    if (is_paragraph and not overlaps_graphic) or CAPTION_RE.match(b[4]):
                        top_limit = max(top_limit, r.y1)
                above = [
                    g for g in graphics
                    if g.y1 <= cap_rect.y0 + 4 and g.y0 >= top_limit - 2
                    and g.x1 > cap_rect.x0 - page_rect.width * 0.3 and g.x0 < cap_rect.x1 + page_rect.width * 0.3
                ]
                if above:
                    region = _union(above)
                else:
                    region = fitz.Rect(page_rect.x0 + 30, top_limit, page_rect.x1 - 30, cap_rect.y0)
                region = (region + (-6, -6, 6, 4)) & page_rect
                if region.height < 60 or region.width < 100:
                    continue
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=region)
                path = out_dir / f"fig{num}.png"
                pix.save(path)
                caption = re.sub(r"\s+", " ", cap[4]).strip()
                figures[num] = {"number": num, "caption": caption[:500], "page": page.number + 1, "path": str(path)}
                if len(figures) >= max_figures:
                    break
            if len(figures) >= max_figures:
                break
        # Always keep a render of the first page as a fallback visual.
        first = doc[0]
        clip = fitz.Rect(first.rect.x0, first.rect.y0, first.rect.x1, first.rect.y0 + first.rect.height * 0.55)
        first.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip).save(out_dir / "page1.png")
    return [figures[k] for k in sorted(figures)]
