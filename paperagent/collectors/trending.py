"""Trending / hot-paper signals for the dashboard.

* hf_daily       - papers the community is discussing today (Hugging Face Daily Papers upvotes)
* active         - most-cited recent papers per keyword (OpenAlex, last 12 months)
* surge          - papers whose citations are accelerating (OpenAlex counts_by_year)
* tracked        - citation growth of papers we reviewed, from our own daily snapshots
                   (Semantic Scholar batch API)
"""
import logging
from datetime import date, timedelta

from .. import config, db
from . import http_get, http_post, openalex

log = logging.getLogger(__name__)

HF_DAILY = "https://huggingface.co/api/daily_papers"
S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"


def hf_daily(limit: int = 30):
    items = http_get(HF_DAILY, params={"limit": 100}).json()
    out = []
    for it in items:
        p = it.get("paper") or {}
        if not p.get("id"):
            continue
        out.append(
            {
                "title": p.get("title") or it.get("title"),
                "arxiv_id": p["id"],
                "url": f"https://arxiv.org/abs/{p['id']}",
                "hf_url": f"https://huggingface.co/papers/{p['id']}",
                "upvotes": p.get("upvotes", 0),
                "comments": it.get("numComments", 0),
                "summary": (p.get("summary") or "")[:900],
                "authors": [a.get("name") for a in (p.get("authors") or [])[:4]],
                "published": p.get("publishedAt"),
            }
        )
    out.sort(key=lambda x: x["upvotes"], reverse=True)
    return out[:limit]


def _slim(p, **extra):
    d = {k: p.get(k) for k in ("title", "url", "pdf_url", "doi", "published", "citation_count", "source_name")}
    d["authors"] = p.get("authors", [])[:4]
    d["summary"] = (p.get("abstract") or "")[:900]
    d.update(extra)
    return d


def active_research(keywords, per_keyword: int = 6):
    since = (date.today() - timedelta(days=365)).isoformat()
    seen, out = set(), []
    for kw in keywords:
        try:
            works = openalex.search_works(kw, since, per_page=per_keyword)
        except Exception as exc:  # one keyword failing shouldn't drop the rest
            log.warning("OpenAlex active search failed for %s: %s", kw, exc)
            continue
        for w in works:
            if w["uid"] in seen:
                continue
            seen.add(w["uid"])
            out.append(_slim(w, keyword=kw))
    out.sort(key=lambda x: x.get("citation_count") or 0, reverse=True)
    return out[:40]


def citation_surge(keywords, per_keyword: int = 25):
    """Rank recent (<3y) papers by citation acceleration: this year's citations
    (annualised) minus last year's."""
    today = date.today()
    since = (today - timedelta(days=3 * 365)).isoformat()
    year_frac = max(today.timetuple().tm_yday / 365.0, 0.08)
    seen, out = set(), []
    for kw in keywords:
        try:
            works = openalex.search_works(kw, since, per_page=per_keyword, extra_filter="cited_by_count:>10")
        except Exception as exc:
            log.warning("OpenAlex surge search failed for %s: %s", kw, exc)
            continue
        for w in works:
            if w["uid"] in seen:
                continue
            seen.add(w["uid"])
            by_year = {c["year"]: c["cited_by_count"] for c in w.get("counts_by_year", [])}
            this_year = by_year.get(today.year, 0)
            last_year = by_year.get(today.year - 1, 0)
            growth = this_year / year_frac - last_year
            if this_year == 0:
                continue
            out.append(
                _slim(
                    w,
                    keyword=kw,
                    this_year=this_year,
                    last_year=last_year,
                    growth=round(growth, 1),
                    growth_pct=round(100 * growth / last_year) if last_year else None,
                )
            )
    out.sort(key=lambda x: x["growth"], reverse=True)
    return out[:40]


def refresh_tracked_citations():
    """Snapshot citation counts of reviewed papers so we can show 7-day growth."""
    papers = db.query_papers(status="done", limit=500)
    ids = {}
    for p in papers:
        if p["uid"].startswith("arxiv:"):
            ids["ARXIV:" + p["uid"][6:]] = p
        elif p.get("doi"):
            ids["DOI:" + p["doi"]] = p
    if not ids:
        return []
    headers = {"x-api-key": config.SEMANTIC_SCHOLAR_API_KEY} if config.SEMANTIC_SCHOLAR_API_KEY else {}
    keys = list(ids)
    for i in range(0, len(keys), 400):
        chunk = keys[i : i + 400]
        resp = http_post(S2_BATCH, json={"ids": chunk}, params={"fields": "citationCount"}, headers=headers)
        for key, item in zip(chunk, resp.json()):
            if not item:
                continue
            p = ids[key]
            count = item.get("citationCount") or 0
            db.snapshot_citations(p["uid"], count)
            db.set_citation_count(p["id"], count)
    out = []
    for p in papers:
        growth = db.citation_growth(p["uid"], days=7)
        if growth:
            review = p.get("review") or {}
            out.append({"id": p["id"], "title": review.get("title_ko") or p["title"], "growth_7d": growth,
                        "citation_count": p.get("citation_count"), "summary": review.get("one_liner") or "",
                        "source_name": p.get("venue") or p.get("source_name")})
    out.sort(key=lambda x: x["growth_7d"], reverse=True)
    return out[:20]


def refresh_all(keywords, logger=log.info):
    """Refresh every trending panel; each source is independent."""
    jobs = {
        "hf_daily": lambda: hf_daily(),
        "active": lambda: active_research(keywords),
        "surge": lambda: citation_surge(keywords),
        "tracked": refresh_tracked_citations,
    }
    for kind, fn in jobs.items():
        try:
            data = fn()
            db.save_trending(kind, data)
            logger(f"trending/{kind}: {len(data)} items")
        except Exception as exc:
            logger(f"trending/{kind} failed: {exc}")
