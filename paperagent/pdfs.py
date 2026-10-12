"""PDF download (saved under the paper title) and figure extraction."""
import logging
import re
from pathlib import Path

import pymupdf as fitz

from .collectors import http_get

log = logging.getLogger(__name__)

CAPTION_RE = re.compile(r"^\s*(Figure|Fig\.?)\s*(\d+)\s*[:.|]", re.IGNORECASE)
MAX_PDF_BYTES = 60 * 1024 * 1024


def safe_filename(title: str, max_len: int = 150) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return (name[:max_len].rstrip() or "untitled") + ".pdf"


def download_pdf(paper: dict, dest_dir: Path) -> Path:
    """Download the paper PDF to dest_dir/<Paper Title>.pdf.

    The pipeline passes a temporary directory, so the PDF only exists while
    Claude reads it and figures are extracted; readers get the original link."""
    if not paper.get("pdf_url"):
        raise ValueError("no open-access PDF url")
    target = Path(dest_dir) / safe_filename(paper["title"])
    if paper["pdf_url"].startswith("storage:"):  # a PDF the admin uploaded
        from . import storage

        item = storage.get(paper["pdf_url"][len("storage:"):])
        if item is None:
            raise ValueError("uploaded PDF is no longer stored; upload it again")
        target.write_bytes(item[0])
        return target
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


ARXIV_IN_TEXT = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;]+[^\s\"<>,;.)])")
_BAD_META_TITLE = re.compile(r"^(untitled|microsoft word|document|paper|main|arxiv|\S+\.(pdf|docx?|tex))", re.IGNORECASE)


def _clean(text):
    return re.sub(r"\s+", " ", (text or "").replace("-\n", "")).strip()


def read_metadata(path: Path) -> dict:
    """Best-effort title, authors, abstract, arXiv id and DOI of an uploaded paper.
    The title is the PDF's own metadata if it looks real, else the largest text
    near the top of page 1; the abstract is the text after an 'Abstract' heading."""
    with fitz.open(path) as doc:
        meta = doc.metadata or {}
        first = doc[0] if doc.page_count else None
        head_text = "\n".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
        title = ""
        if first is not None:
            spans = [(round(s["size"], 1), s["text"].strip(), line["bbox"][1])
                     for b in first.get_text("dict")["blocks"] for line in b.get("lines", []) for s in line["spans"]
                     if s["text"].strip() and line["bbox"][1] < first.rect.height * 0.5]
            if spans:
                biggest = max(size for size, text, _ in spans if len(text) > 3) if any(len(t) > 3 for _, t, _ in spans) else 0
                title = _clean(" ".join(text for size, text, _ in sorted(spans, key=lambda x: x[2]) if size == biggest))
    meta_title = _clean(meta.get("title"))
    if len(meta_title) > 12 and not _BAD_META_TITLE.match(meta_title):
        title = meta_title
    m = re.search(r"\bA\s?BSTRACT\b[\s.:—–-]*(.+?)(?=\n\s*(?:\d\.?|I\.?)?\s*(?:Introduction|INTRODUCTION|Keywords|Index Terms)\b)",
                  head_text, re.DOTALL | re.IGNORECASE)
    abstract = _clean(m.group(1))[:3000] if m else _clean(head_text)[:1500]
    authors = [a.strip() for a in re.split(r"[;,]| and ", meta.get("author") or "") if a.strip()][:20]
    arxiv = ARXIV_IN_TEXT.search(head_text)
    doi = DOI_IN_TEXT.search(head_text)
    return {"title": title[:300] or None, "authors": authors, "abstract": abstract,
            "arxiv_id": arxiv.group(1) if arxiv else None, "doi": doi.group(1) if doi else None}


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


def _figure_graphics(graphics, cap_rect, top_limit, gap=24):
    """Graphics belonging to the figure above a caption: start from those
    horizontally overlapping the caption, then grow to connected ones (so wide
    figures with a short caption stay whole, while the other column's figure,
    separated by a gap, is left out)."""
    band = [g for g in graphics if g.y1 <= cap_rect.y0 + 4 and g.y0 >= top_limit - 2]
    picked = [g for g in band if g.x1 > cap_rect.x0 - 20 and g.x0 < cap_rect.x1 + 20] or band
    if not picked:
        return []
    region = _union(picked)
    rest = [g for g in band if g not in picked]
    grew = True
    while grew and rest:
        grew = False
        near = region + (-gap, -gap, gap, gap)
        for g in list(rest):
            if g.intersects(near):
                region |= g
                picked.append(g)
                rest.remove(g)
                grew = True
    return picked


def _is_paragraph(block, page_rect) -> bool:
    """Body text: wide, several lines and sentence-length (not a row of tick labels)."""
    text = block[4]
    return fitz.Rect(block[:4]).width > 0.35 * page_rect.width and text.count("\n") >= 3 and len(text) > 150


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
                    is_paragraph = _is_paragraph(b, page_rect)
                    overlaps_graphic = any(r.intersects(g) for g in graphics)
                    if (is_paragraph and not overlaps_graphic) or CAPTION_RE.match(b[4]):
                        top_limit = max(top_limit, r.y1)
                above = _figure_graphics(graphics, cap_rect, top_limit)
                if above:
                    region = _union(above)
                    # Pull in short labels that sit just outside the drawing
                    # (axis ticks, axis titles, legends), but not body text.
                    for b in blocks:
                        r = fitz.Rect(b[:4])
                        if b is cap or CAPTION_RE.match(b[4]) or _is_paragraph(b, page_rect) or r.y1 > cap_rect.y0 + 2:
                            continue
                        near = region + (-40, -14, 40, 30)
                        if r.intersects(near) and r.y0 >= top_limit - 2:
                            region |= r
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
