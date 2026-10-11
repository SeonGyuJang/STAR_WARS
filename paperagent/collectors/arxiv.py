"""arXiv collector: queries the official arXiv API (Atom feed) and parses it with
BeautifulSoup. arXiv asks automated clients to use export.arxiv.org and to wait
~3 seconds between requests, so we do not scrape the HTML listing pages."""
import re
import time
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from . import http_get, match_keywords

API_URL = "https://export.arxiv.org/api/query"
REQUEST_GAP_SECONDS = 3.1
_last_request = 0.0


def _throttle():
    global _last_request
    wait = REQUEST_GAP_SECONDS - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def build_query(category: str, keywords) -> str:
    kw = " OR ".join(f'all:"{k}"' if " " in k else f"all:{k}" for k in keywords)
    return f"cat:{category} AND ({kw})" if kw else f"cat:{category}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_feed(xml_text: str):
    soup = BeautifulSoup(xml_text, "xml")
    papers = []
    for entry in soup.find_all("entry"):
        abs_url = _clean(entry.find("id").text)
        m = re.search(r"arxiv\.org/abs/([^v\s]+)(v\d+)?$", abs_url)
        if not m:
            continue  # error entries have a different id
        arxiv_id = m.group(1)
        pdf_link = entry.find("link", attrs={"title": "pdf"})
        doi = entry.find("arxiv:doi") or entry.find("doi")
        primary = entry.find("arxiv:primary_category") or entry.find("primary_category")
        papers.append(
            {
                "uid": f"arxiv:{arxiv_id}",
                "arxiv_id": arxiv_id,
                "source_kind": "arxiv",
                "title": _clean(entry.find("title").text),
                "abstract": _clean(entry.find("summary").text),
                "authors": [_clean(a.find("name").text) for a in entry.find_all("author")],
                "published": _clean(entry.find("published").text),
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "pdf_url": pdf_link["href"].replace("http://", "https://") if pdf_link else f"https://arxiv.org/pdf/{arxiv_id}",
                "doi": _clean(doi.text) if doi else None,
                "categories": [c["term"] for c in entry.find_all("category")],
                "primary_category": primary["term"] if primary else None,
            }
        )
    return papers


def fetch_category(category: str, keywords, lookback_days: int = 3, max_results: int = 40):
    """Newest papers in `category` matching any keyword, submitted within `lookback_days`."""
    _throttle()
    resp = http_get(
        API_URL,
        params={
            "search_query": build_query(category, keywords),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": 0,
            "max_results": max_results,
        },
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    out = []
    for p in parse_feed(resp.text):
        try:
            published = datetime.fromisoformat(p["published"].replace("Z", "+00:00"))
        except ValueError:
            published = None
        if published and published < cutoff:
            continue
        p["matched_keywords"] = match_keywords(f"{p['title']} {p['abstract']}", keywords)
        if keywords and not p["matched_keywords"]:
            continue
        out.append(p)
    return out


def fetch_by_ids(arxiv_ids):
    """Metadata for specific arXiv ids (used for trending papers)."""
    if not arxiv_ids:
        return []
    _throttle()
    resp = http_get(API_URL, params={"id_list": ",".join(arxiv_ids), "max_results": len(arxiv_ids)})
    return parse_feed(resp.text)
