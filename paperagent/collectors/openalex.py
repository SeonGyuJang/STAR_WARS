"""OpenAlex collector for journals/conferences and citation data.

Google Scholar has no API and blocks automated access, so journals are tracked
through OpenAlex (free, no key, covers Crossref + arXiv + publisher metadata)."""
from datetime import date, timedelta

from .. import config
from . import http_get, match_keywords

API = "https://api.openalex.org"


def _params(extra=None):
    p = dict(extra or {})
    if config.OPENALEX_MAILTO:
        p["mailto"] = config.OPENALEX_MAILTO
    return p


def short_id(openalex_url: str) -> str:
    return (openalex_url or "").rsplit("/", 1)[-1]


def search_sources(name: str, limit: int = 8):
    """Candidate journals/conferences for a free-text name."""
    resp = http_get(f"{API}/sources", params=_params({"search": name, "per-page": limit}))
    return [
        {
            "id": short_id(s["id"]),
            "name": s["display_name"],
            "type": s.get("type"),
            "publisher": s.get("host_organization_name"),
            "works_count": s.get("works_count"),
        }
        for s in resp.json().get("results", [])
    ]


def resolve_source(name: str):
    results = search_sources(name, limit=1)
    return results[0] if results else None


def reconstruct_abstract(inverted_index):
    if not inverted_index:
        return ""
    positions = []
    for word, idxs in inverted_index.items():
        positions += [(i, word) for i in idxs]
    return " ".join(w for _, w in sorted(positions))


def _arxiv_id(work):
    for loc in work.get("locations") or []:
        url = (loc.get("landing_page_url") or "") + " " + (loc.get("pdf_url") or "")
        if "arxiv.org/abs/" in url:
            return url.split("arxiv.org/abs/")[1].split()[0].split("v")[0]
    return None


def work_to_paper(work, source_name=None):
    doi = (work.get("doi") or "").replace("https://doi.org/", "") or None
    best = work.get("best_oa_location") or {}
    oa = work.get("open_access") or {}
    pdf_url = best.get("pdf_url") or (oa.get("oa_url") if (oa.get("oa_url") or "").endswith(".pdf") else None)
    arxiv_id = _arxiv_id(work)
    if not pdf_url and arxiv_id:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    primary = work.get("primary_location") or {}
    src = primary.get("source") or {}
    return {
        "uid": f"doi:{doi.lower()}" if doi else f"openalex:{short_id(work['id'])}",
        "source_kind": "journal",
        "source_name": source_name or src.get("display_name"),
        "title": work.get("display_name") or work.get("title") or "(untitled)",
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "authors": [a["author"]["display_name"] for a in work.get("authorships", []) if a.get("author")],
        "published": work.get("publication_date"),
        "url": primary.get("landing_page_url") or work.get("doi") or work["id"],
        "pdf_url": pdf_url,
        "doi": doi,
        "categories": [c["display_name"] for c in (work.get("concepts") or work.get("topics") or [])[:5]],
        "citation_count": work.get("cited_by_count"),
        "counts_by_year": work.get("counts_by_year") or [],
        "openalex_id": short_id(work["id"]),
    }


def fetch_journal(source_id: str, source_name: str, keywords, lookback_days: int = 7, max_results: int = 40):
    since = (date.today() - timedelta(days=max(lookback_days, 7))).isoformat()
    resp = http_get(
        f"{API}/works",
        params=_params(
            {
                "filter": f"primary_location.source.id:{source_id},from_publication_date:{since}",
                "sort": "publication_date:desc",
                "per-page": max_results,
            }
        ),
    )
    out = []
    for work in resp.json().get("results", []):
        p = work_to_paper(work, source_name)
        p["matched_keywords"] = match_keywords(f"{p['title']} {p['abstract']}", keywords)
        if keywords and not p["matched_keywords"]:
            continue
        out.append(p)
    return out


def search_works(query: str, from_date: str, sort: str = "cited_by_count:desc", per_page: int = 25, extra_filter=""):
    flt = f"from_publication_date:{from_date}"
    if extra_filter:
        flt += "," + extra_filter
    resp = http_get(
        f"{API}/works",
        params=_params({"search": query, "filter": flt, "sort": sort, "per-page": per_page}),
    )
    return [work_to_paper(w) for w in resp.json().get("results", [])]
