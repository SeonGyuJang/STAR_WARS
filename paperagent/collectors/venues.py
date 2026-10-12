"""Journals and conferences: finding them by name and collecting their papers.

A venue is collected two ways, and either alone is enough:

* arXiv  - most ML/CS papers are on arXiv, and authors note the venue in the
           comments or journal-ref field ("Accepted at NeurIPS 2025"). This is
           how conference papers show up within days, long before proceedings
           are indexed anywhere, and it comes with a free PDF.
* OpenAlex - the venue's own listing (journals, and proceedings once indexed).

Conferences are known by short names (NeurIPS, ICML, ...) that OpenAlex's
source search does not match, so well-known venues are listed here with their
full names and the words to look for in arXiv comments.
"""
import logging
import re
from datetime import date, datetime, timedelta, timezone

from . import arxiv, match_keywords, openalex

log = logging.getLogger(__name__)

# key, short name, full name, type, extra aliases[, arXiv terms]. Unless given,
# the arXiv terms are the short name, the aliases and the full name.
_KNOWN = [
    ("neurips", "NeurIPS", "Neural Information Processing Systems", "conference", ["NIPS"]),
    ("icml", "ICML", "International Conference on Machine Learning", "conference", []),
    ("iclr", "ICLR", "International Conference on Learning Representations", "conference", []),
    ("cvpr", "CVPR", "IEEE/CVF Conference on Computer Vision and Pattern Recognition", "conference", []),
    ("iccv", "ICCV", "IEEE/CVF International Conference on Computer Vision", "conference", []),
    ("eccv", "ECCV", "European Conference on Computer Vision", "conference", []),
    ("wacv", "WACV", "IEEE/CVF Winter Conference on Applications of Computer Vision", "conference", []),
    ("acl", "ACL", "Annual Meeting of the Association for Computational Linguistics", "conference", []),
    ("emnlp", "EMNLP", "Conference on Empirical Methods in Natural Language Processing", "conference", []),
    ("naacl", "NAACL", "North American Chapter of the Association for Computational Linguistics", "conference", []),
    ("coling", "COLING", "International Conference on Computational Linguistics", "conference", []),
    ("aaai", "AAAI", "AAAI Conference on Artificial Intelligence", "conference", []),
    ("ijcai", "IJCAI", "International Joint Conference on Artificial Intelligence", "conference", []),
    ("kdd", "KDD", "ACM SIGKDD Conference on Knowledge Discovery and Data Mining", "conference", ["SIGKDD"]),
    ("www", "WWW", "The Web Conference", "conference", ["TheWebConf"], ["TheWebConf", "The Web Conference"]),
    ("sigir", "SIGIR", "International ACM SIGIR Conference on Research and Development in Information Retrieval", "conference", []),
    ("chi", "CHI", "CHI Conference on Human Factors in Computing Systems", "conference", []),
    ("aistats", "AISTATS", "International Conference on Artificial Intelligence and Statistics", "conference", []),
    ("uai", "UAI", "Conference on Uncertainty in Artificial Intelligence", "conference", []),
    ("colt", "COLT", "Conference on Learning Theory", "conference", []),
    ("corl", "CoRL", "Conference on Robot Learning", "conference", []),
    ("icra", "ICRA", "IEEE International Conference on Robotics and Automation", "conference", []),
    ("iros", "IROS", "IEEE/RSJ International Conference on Intelligent Robots and Systems", "conference", []),
    ("rss", "RSS", "Robotics: Science and Systems", "conference", []),
    ("interspeech", "Interspeech", "Interspeech", "conference", []),
    ("icassp", "ICASSP", "IEEE International Conference on Acoustics, Speech and Signal Processing", "conference", []),
    ("miccai", "MICCAI", "Medical Image Computing and Computer Assisted Intervention", "conference", []),
    ("colm", "COLM", "Conference on Language Modeling", "conference", []),
    ("tmlr", "TMLR", "Transactions on Machine Learning Research", "journal", []),
    ("jmlr", "JMLR", "Journal of Machine Learning Research", "journal", []),
    ("tpami", "TPAMI", "IEEE Transactions on Pattern Analysis and Machine Intelligence", "journal", ["T-PAMI", "PAMI"]),
    ("ijcv", "IJCV", "International Journal of Computer Vision", "journal", []),
    ("natmachintell", "Nature Machine Intelligence", "Nature Machine Intelligence", "journal", ["Nat. Mach. Intell."]),
    ("tacl", "TACL", "Transactions of the Association for Computational Linguistics", "journal", []),
]

TYPE_LABEL = {"conference": "학회", "journal": "저널", "repository": "저장소", "ebook platform": "기타", "book series": "기타"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _venue(key, short, full, vtype, aliases, terms=None):
    terms = terms or list(dict.fromkeys([short, *aliases, full]))
    return {"key": key, "short": short, "full": full, "type": vtype, "aliases": aliases, "arxiv_terms": terms}


KNOWN = {row[0]: _venue(*row) for row in _KNOWN}


def find_known(query: str):
    """A well-known venue matching a typed name: 'NeurIPS', 'neurips 2025',
    'Neural Information Processing Systems', 'T-PAMI', ..."""
    q = _norm(re.sub(r"\b(19|20)\d{2}\b|'\d{2}\b", "", query or ""))
    if not q:
        return None
    for v in KNOWN.values():
        names = [v["key"], v["short"], v["full"], *v["aliases"]]
        if any(q == _norm(n) for n in names):
            return v
    for v in KNOWN.values():  # "advances in neural information processing systems", "proceedings of icml"
        if len(q) >= 6 and (_norm(v["full"]) in q or q in _norm(v["full"])):
            return v
    return None


def search(query: str, limit: int = 8):
    """Candidates for the settings page: a known venue first (it works even if
    OpenAlex is unreachable), then OpenAlex sources. Each candidate says how
    it will be collected."""
    out = []
    known = find_known(query)
    if known:
        out.append({"name": known["short"], "full": known["full"], "type": known["type"], "key": known["key"],
                    "openalex_id": None, "works_count": None, "via": "arxiv+openalex"})
    error = None
    try:
        seen = set()
        for q in ([known["full"]] if known else []) + [query]:
            for s in openalex.search_sources(q, limit=limit):
                if s["id"] in seen:
                    continue
                seen.add(s["id"])
                out.append({"name": s["name"], "full": s["name"], "type": s.get("type") or "journal", "key": None,
                            "openalex_id": s["id"], "works_count": s.get("works_count"),
                            "publisher": s.get("publisher"), "via": "openalex"})
    except Exception as exc:  # OpenAlex down or blocked: known venues still work
        log.warning("OpenAlex source search failed for %r: %s", query, exc)
        error = str(exc)
    return out[: limit + 1], error


def best_openalex_match(venue: dict):
    """The OpenAlex source for a known venue, or None. Prefers a source whose
    name contains the venue's full name, then the one with the most works."""
    results = openalex.search_sources(venue["full"], limit=10)  # network errors go to the caller, who retries next run
    full = _norm(venue["full"])
    hits = [s for s in results if full in _norm(s["name"]) or _norm(venue["short"]) == _norm(s["name"])]
    hits.sort(key=lambda s: s.get("works_count") or 0, reverse=True)
    return hits[0] if hits else None


# --- collecting ----------------------------------------------------------------

def mentions(text: str, terms) -> bool:
    """Whether `text` names the venue. Acronyms must match case ('NeurIPS', not
    'neurips' inside a word or URL); multi-word names match any case."""
    text = text or ""
    for t in terms:
        if " " in t:
            if t.lower() in text.lower():
                return True
        elif re.search(r"(?<![A-Za-z0-9./])" + re.escape(t) + r"(?![A-Za-z])", text):
            return True
    return False


def build_arxiv_query(terms, keywords) -> str:
    venue = " OR ".join(f'{f}:"{t}"' for t in terms for f in ("co", "jr"))
    kw = " OR ".join(f'all:"{k}"' if " " in k else f"all:{k}" for k in keywords)
    return f"({venue}) AND ({kw})" if kw else f"({venue})"


def fetch_arxiv(name: str, terms, keywords, lookback_days: int, max_results: int):
    """arXiv papers whose comments or journal-ref name the venue, newest updates first."""
    if not terms:
        return []
    arxiv._throttle()
    resp = arxiv.http_get(arxiv.API_URL, params={
        "search_query": build_arxiv_query(terms, keywords),
        "sortBy": "lastUpdatedDate", "sortOrder": "descending", "start": 0, "max_results": max_results * 2,
    })
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    out = []
    for p in arxiv.parse_feed(resp.text):
        note = f"{p.get('comment') or ''} {p.get('journal_ref') or ''}"
        if not mentions(note, terms):  # the API matches loosely; keep only real mentions
            continue
        stamp = p.get("updated") or p.get("published") or ""
        try:
            if datetime.fromisoformat(stamp.replace("Z", "+00:00")) < cutoff:
                continue
        except ValueError:
            pass
        p["matched_keywords"] = match_keywords(f"{p['title']} {p['abstract']}", keywords)
        if keywords and not p["matched_keywords"]:
            continue
        out.append(p)
        if len(out) >= max_results:
            break
    return out


def fetch_openalex(openalex_id: str, keywords, lookback_days: int, max_results: int):
    """Works listed under the venue in OpenAlex - in any location, so papers whose
    primary copy is on arXiv count too."""
    since = (date.today() - timedelta(days=lookback_days)).isoformat()
    resp = openalex.http_get(f"{openalex.API}/works", params=openalex._params({
        "filter": f"locations.source.id:{openalex_id},from_publication_date:{since}",
        "sort": "publication_date:desc", "per-page": max_results,
    }))
    out = []
    for work in resp.json().get("results", []):
        p = openalex.work_to_paper(work)
        p["matched_keywords"] = match_keywords(f"{p['title']} {p['abstract']}", keywords)
        if keywords and not p["matched_keywords"]:
            continue
        out.append(p)
    return out


def lookback_for(vtype: str, lookback_days: int) -> int:
    """Journals publish a few papers a week and conferences in yearly batches, so a
    3-day window would usually find nothing. Already-known papers are skipped,
    so a longer window costs nothing."""
    return max(lookback_days, 400 if vtype == "conference" else 45)
