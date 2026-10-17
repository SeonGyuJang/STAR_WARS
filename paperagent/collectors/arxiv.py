"""arXiv collector: queries the official arXiv API (Atom feed) and parses it with
BeautifulSoup. arXiv asks automated clients to use export.arxiv.org and to wait
~3 seconds between requests, so we do not scrape the HTML listing pages.

arXiv answers busy or too-frequent clients with 429 (or 503) - often on shared
cloud IPs such as Heroku's. So:
  * all categories go into ONE query per run instead of one each,
  * a 429/503 is retried after the Retry-After it sends (or 15 s, 45 s),
  * if the API still refuses, each category's daily RSS feed
    (rss.arxiv.org, a different service) is used instead."""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

import requests

from . import http_get, match_keywords

log = logging.getLogger(__name__)
API_URL = "https://export.arxiv.org/api/query"
RSS_URL = "https://rss.arxiv.org/rss/{category}"
RETRY_WAITS = (15, 45)  # seconds, when arXiv does not say how long to wait

# Common AI/CS categories, for the settings page (any valid code can be added).
CATEGORY_NAMES = {
    "cs.AI": "Artificial Intelligence", "cs.CL": "Computation and Language", "cs.CV": "Computer Vision",
    "cs.LG": "Machine Learning", "stat.ML": "Machine Learning (Statistics)", "cs.RO": "Robotics",
    "cs.IR": "Information Retrieval", "cs.NE": "Neural and Evolutionary Computing", "cs.HC": "Human-Computer Interaction",
    "cs.MA": "Multiagent Systems", "cs.CR": "Cryptography and Security", "cs.SE": "Software Engineering",
    "cs.DB": "Databases", "cs.DC": "Distributed Computing", "cs.SD": "Sound", "cs.GR": "Graphics",
    "cs.CY": "Computers and Society", "eess.AS": "Audio and Speech Processing", "eess.IV": "Image and Video Processing",
    "q-bio.QM": "Quantitative Methods (Biology)",
}
REQUEST_GAP_SECONDS = 5.0
_last_request = 0.0


def _throttle():
    global _last_request
    wait = REQUEST_GAP_SECONDS - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


class ArxivBusy(RuntimeError):
    """arXiv kept answering 429/503."""


def api_get(params):
    """One arXiv API request: spaced out, and retried when arXiv says it is busy."""
    waits = list(RETRY_WAITS)
    while True:
        _throttle()
        try:
            return http_get(API_URL, params=params, retries=1)
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status not in (429, 503):
                raise
            if not waits:
                raise ArxivBusy(f"arXiv API is busy ({status}) - too many requests from this server") from exc
            after = (exc.response.headers or {}).get("Retry-After") if exc.response is not None else None
            wait = min(int(after), 120) if after and str(after).isdigit() else waits[0]
            waits.pop(0)
            log.warning("arXiv API %s; retrying in %ss", status, wait)
            time.sleep(wait)


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
        comment = entry.find("arxiv:comment") or entry.find("comment")
        journal_ref = entry.find("arxiv:journal_ref") or entry.find("journal_ref")
        updated = entry.find("updated")
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
                "comment": _clean(comment.text) if comment else None,
                "journal_ref": _clean(journal_ref.text) if journal_ref else None,
                "updated": _clean(updated.text) if updated else None,
            }
        )
    return papers


def _recent_matches(papers, keywords, lookback_days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    out = []
    for p in papers:
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


def fetch_category(category: str, keywords, lookback_days: int = 3, max_results: int = 40):
    """Newest papers in `category` matching any keyword, submitted within `lookback_days`."""
    return fetch_categories([category], keywords, lookback_days, max_results)


def fetch_categories(categories, keywords, lookback_days: int = 3, max_per_category: int = 40):
    """Newest papers in any of `categories` matching any keyword - one API request for all of them."""
    cats = " OR ".join(f"cat:{c}" for c in categories)
    kw = " OR ".join(f'all:"{k}"' if " " in k else f"all:{k}" for k in keywords)
    query = f"({cats}) AND ({kw})" if kw else f"({cats})"
    resp = api_get({"search_query": query if len(categories) > 1 else build_query(categories[0], keywords),
                    "sortBy": "submittedDate", "sortOrder": "descending", "start": 0,
                    "max_results": min(max_per_category * len(categories), 300)})
    return _recent_matches(parse_feed(resp.text), keywords, lookback_days)


def parse_rss(xml_text: str):
    """The daily announcement feed (rss.arxiv.org). Only new papers and
    cross-lists; replacements of old papers are left out."""
    soup = BeautifulSoup(xml_text, "xml")
    papers = []
    for item in soup.find_all("item"):
        link = _clean(item.find("link").text if item.find("link") else "")
        m = re.search(r"arxiv\.org/abs/([^v\s]+)(v\d+)?", link)
        if not m:
            continue
        desc = _clean(item.find("description").text if item.find("description") else "")
        kind = re.search(r"Announce Type:\s*([\w-]+)", desc)
        if kind and kind.group(1).startswith("replace"):
            continue
        abstract = re.sub(r"^.*?Abstract:\s*", "", desc) if "Abstract:" in desc else desc
        arxiv_id = m.group(1)
        creator = item.find("dc:creator") or item.find("creator")
        cats = [_clean(c.text) for c in item.find_all("category")]
        when = item.find("pubDate")
        published = None
        if when:
            try:
                published = datetime.strptime(_clean(when.text), "%a, %d %b %Y %H:%M:%S %z").isoformat()
            except ValueError:
                published = None
        papers.append({
            "uid": f"arxiv:{arxiv_id}", "arxiv_id": arxiv_id, "source_kind": "arxiv",
            "title": _clean(item.find("title").text), "abstract": abstract,
            "authors": [a.strip() for a in _clean(creator.text).split(",") if a.strip()] if creator else [],
            "published": published or datetime.now(timezone.utc).isoformat(),
            "url": f"https://arxiv.org/abs/{arxiv_id}", "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
            "doi": None, "categories": cats, "primary_category": cats[0] if cats else None,
            "comment": None, "journal_ref": None, "updated": None,
        })
    return papers


def fetch_rss(category: str, keywords, lookback_days: int = 3):
    """Fallback when the API is busy: the category's latest announcements."""
    resp = http_get(RSS_URL.format(category=category), retries=2)
    return _recent_matches(parse_rss(resp.text), keywords, lookback_days)


def fetch_by_ids(arxiv_ids):
    """Metadata for specific arXiv ids (used for trending papers)."""
    if not arxiv_ids:
        return []
    resp = api_get({"id_list": ",".join(arxiv_ids), "max_results": len(arxiv_ids)})
    return parse_feed(resp.text)
