"""Trending projects and tech news for the TECH card news.

  github    GitHub Trending (the page has no API: the repository rows are read
            from the HTML; if that fails, the search API's most-starred new
            repositories stand in)
  hn        Hacker News front page (official Firebase API)
  geeknews  GeekNews (news.hada.io), Korean tech news with summaries (RSS)
  hf        Hugging Face trending models and Spaces (hub API)

Each collector returns items {uid, source, title, url, summary, meta}. Before
a post is written, fetch_context() reads the project's README / the article /
the model card and finds pictures and demo clips (media_candidates).
"""
import logging
import re
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .. import config
from . import http_get

log = logging.getLogger(__name__)

SOURCES = {
    "github": {"name": "GitHub Trending", "short": "GitHub", "url": "https://github.com/trending"},
    "hn": {"name": "Hacker News", "short": "Hacker News", "url": "https://news.ycombinator.com/"},
    "geeknews": {"name": "GeekNews", "short": "GeekNews", "url": "https://news.hada.io/"},
    "hf": {"name": "Hugging Face 트렌딩", "short": "Hugging Face", "url": "https://huggingface.co/models?sort=trending"},
}

GITHUB_TRENDING = "https://github.com/trending"
GITHUB_API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
HN_API = "https://hacker-news.firebaseio.com/v0"
GEEKNEWS_FEEDS = ["https://news.hada.io/rss/news", "https://feeds.feedburner.com/geeknews-feed"]
HF_API = "https://huggingface.co/api"
CONTEXT_CHARS = 40000  # per source read (README, article, model card)
TOTAL_CHARS = 90000  # everything handed to the writer for one post


def _int(text) -> int | None:
    m = re.search(r"[\d,]+", text or "")
    return int(m.group(0).replace(",", "")) if m else None


def compact(n) -> str:
    """12345 -> '1.2만', 2400 -> '2,400' (Korean style for card numbers)."""
    if n is None:
        return "-"
    n = int(n)
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}억".replace(".0억", "억")
    if n >= 10_000:
        return f"{n / 10_000:.1f}만".replace(".0만", "만")
    return f"{n:,}"


# --- GitHub ----------------------------------------------------------------------

def parse_github_trending(html: str, limit: int = 15):
    soup = BeautifulSoup(html, "lxml")
    out = []
    for row in soup.select("article.Box-row"):
        link = row.select_one("h2 a[href]")
        if not link:
            continue
        full = link["href"].strip("/")
        if full.count("/") != 1:
            continue
        desc = row.select_one("p")
        lang = row.select_one('[itemprop="programmingLanguage"]')
        stars = row.select_one('a[href$="/stargazers"]')
        forks = row.select_one('a[href$="/forks"]')
        today = next((s for s in row.select("span") if "stars" in s.get_text() and ("today" in s.get_text() or "this week" in s.get_text())), None)
        out.append({
            "uid": f"github:{full.lower()}", "source": "github", "title": full,
            "url": f"https://github.com/{full}",
            "summary": desc.get_text(" ", strip=True) if desc else "",
            "meta": {"stars": _int(stars.get_text()) if stars else None, "forks": _int(forks.get_text()) if forks else None,
                     "stars_today": _int(today.get_text()) if today else None,
                     "language": lang.get_text(strip=True) if lang else None},
        })
        if len(out) >= limit:
            break
    return out


def github_trending(limit: int = 15):
    try:
        items = parse_github_trending(http_get(GITHUB_TRENDING, params={"since": "daily"}, retries=2).text, limit)
        if items:
            return items
        log.warning("GitHub Trending page had no repository rows; using the search API")
    except Exception as exc:
        log.warning("GitHub Trending page failed (%s); using the search API", exc)
    return github_search(limit)


def _gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if config.GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return h


def github_search(limit: int = 15):
    """Most-starred repositories created in the last week - close to 'trending'."""
    since = (date.today() - timedelta(days=7)).isoformat()
    data = http_get(f"{GITHUB_API}/search/repositories", params={"q": f"created:>{since}", "sort": "stars", "order": "desc",
                                                                   "per_page": limit}, headers=_gh_headers(), retries=2).json()
    out = []
    for r in data.get("items", [])[:limit]:
        out.append({
            "uid": f"github:{r['full_name'].lower()}", "source": "github", "title": r["full_name"], "url": r["html_url"],
            "summary": r.get("description") or "",
            "meta": {"stars": r.get("stargazers_count"), "forks": r.get("forks_count"), "language": r.get("language"),
                     "topics": r.get("topics", [])[:6], "new_this_week": True},
        })
    return out


# --- Hacker News -----------------------------------------------------------------

def hacker_news(limit: int = 15, min_points: int = 120, scan: int = 40):
    ids = http_get(f"{HN_API}/topstories.json", retries=2).json()[:scan]
    out = []
    for i in ids:
        try:
            it = http_get(f"{HN_API}/item/{i}.json", retries=2, timeout=20).json() or {}
        except Exception:
            continue
        if it.get("type") != "story" or it.get("dead") or it.get("deleted") or (it.get("score") or 0) < min_points:
            continue
        discussion = f"https://news.ycombinator.com/item?id={i}"
        text = BeautifulSoup(it.get("text") or "", "lxml").get_text(" ", strip=True)
        out.append({
            "uid": f"hn:{i}", "source": "hn", "title": it.get("title") or "", "url": it.get("url") or discussion,
            "summary": text[:600],
            "meta": {"points": it.get("score"), "comments": it.get("descendants"), "author": it.get("by"),
                     "discussion": discussion, "time": it.get("time")},
        })
        if len(out) >= limit:
            break
    return out


# --- GeekNews --------------------------------------------------------------------

def parse_geeknews_feed(xml: str, limit: int = 15):
    """GeekNews Atom (or RSS) feed: Korean title, the topic link and its summary."""
    soup = BeautifulSoup(xml, "xml")
    out = []
    for e in soup.find_all("entry") or soup.find_all("item"):
        title = e.find("title").get_text(strip=True) if e.find("title") else ""
        link_el = e.find("link")
        link = (link_el.get("href") or link_el.get_text(strip=True)) if link_el else ""
        ident = e.find("id") or e.find("guid")
        m = re.search(r"id=(\d+)", link) or (re.search(r"id=(\d+)", ident.get_text()) if ident else None)
        if not title or not m:
            continue
        body = e.find("content") or e.find("description") or e.find("summary")
        summary = BeautifulSoup(body.get_text() if body else "", "lxml").get_text(" ", strip=True)
        when = e.find("published") or e.find("updated") or e.find("pubDate")
        topic = f"https://news.hada.io/topic?id={m.group(1)}"
        out.append({
            "uid": f"geeknews:{m.group(1)}", "source": "geeknews", "title": title, "url": topic, "summary": summary[:1200],
            "meta": {"discussion": topic, "published": when.get_text(strip=True)[:32] if when else None},
        })
        if len(out) >= limit:
            break
    return out


def geeknews(limit: int = 15):
    last = None
    for feed in GEEKNEWS_FEEDS:
        try:
            items = parse_geeknews_feed(http_get(feed, retries=2).text, limit)
            if items:
                return items
        except Exception as exc:
            last = exc
    if last:
        raise last
    return []


# --- Hugging Face ----------------------------------------------------------------

def hugging_face(limit: int = 12):
    out = []
    for kind, path in (("model", "models"), ("space", "spaces")):
        try:
            rows = http_get(f"{HF_API}/{path}", params={"sort": "trendingScore", "direction": -1, "limit": limit}, retries=2).json()
        except Exception as exc:
            log.warning("Hugging Face %s failed: %s", path, exc)
            continue
        for r in rows[: limit // 2 + 1]:
            rid = r.get("id") or r.get("modelId")
            if not rid:
                continue
            base = "https://huggingface.co/" + (f"spaces/{rid}" if kind == "space" else rid)
            out.append({
                "uid": f"hf:{kind}/{rid.lower()}", "source": "hf", "title": rid, "url": base,
                "summary": (r.get("cardData") or {}).get("short_description") or r.get("pipeline_tag") or "",
                "meta": {"kind": kind, "likes": r.get("likes"), "downloads": r.get("downloads"),
                         "task": r.get("pipeline_tag"), "sdk": r.get("sdk"), "trending": r.get("trendingScore")},
            })
    out.sort(key=lambda x: x["meta"].get("trending") or 0, reverse=True)
    return out[:limit]


COLLECTORS = {"github": github_trending, "hn": hacker_news, "geeknews": geeknews, "hf": hugging_face}


def collect(sources, limit: int = 15, say=log.info):
    """Items from the enabled sources; a source that fails is reported and skipped."""
    out = []
    for key in sources:
        fn = COLLECTORS.get(key)
        if not fn:
            continue
        try:
            items = fn(limit)
            say(f"{SOURCES[key]['name']}: {len(items)} items")
            out.extend(items)
        except Exception as exc:
            say(f"{SOURCES[key]['name']} failed: {exc}")
    return out


# --- context and media -------------------------------------------------------------

_IMG_MD = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_SRC_HTML = re.compile(r"<(?:img|video|source)[^>]+src=[\"']([^\"']+)[\"']", re.I)
_ASSET = re.compile(r"https://github\.com/user-attachments/assets/[0-9a-f\-]{36}")
_BADGE = re.compile(r"shields\.io|badge|travis-ci|codecov|circleci|github\.com/.+/actions|img\.shields|badgen|"
                    r"star-history|contrib\.rocks|visitor|hits\.|komarev|\.svg($|\?)|discord|twitter|x\.com|"
                    r"buymeacoffee|ko-fi|sponsor|trendshift|producthunt", re.I)


def _near(text: str, pos: int, chars: int = 260) -> str:
    """The plain text just before a position (the heading/sentence that introduces a clip)."""
    before = text[max(0, pos - chars * 3):pos]
    before = re.sub(r"<[^>]+>|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)|https?://\S+", r"\1", before)
    before = re.sub(r"[#*_`>|-]+", " ", before)
    return re.sub(r"\s+", " ", before).strip()[-chars:]


def clip_notes(text: str, base: str, videos) -> dict:
    """{clip url: text just before it} so the writer can tell what each clip shows."""
    notes = {}
    for m in list(_IMG_MD.finditer(text)) + list(_SRC_HTML.finditer(text)) + list(_ASSET.finditer(text)):
        u = urljoin(base, (m.group(1) if m.groups() else m.group(0)).strip())
        if u in videos and u not in notes:
            notes[u] = _near(text, m.start())
    return notes


def media_candidates(text: str, base: str):
    """Picture and clip URLs in a README / model card, in order, without badges.
    Returns (images, videos)."""
    urls = []
    for m in list(_IMG_MD.finditer(text)) + list(_SRC_HTML.finditer(text)) + list(_ASSET.finditer(text)):
        urls.append((m.start(), m.group(1) if m.groups() else m.group(0)))
    images, videos = [], []
    for _, u in sorted(urls):
        u = u.strip()
        if not u or u.startswith("data:") or _BADGE.search(u):
            continue
        u = urljoin(base, u)
        low = urlparse(u).path.lower()
        if low.endswith((".mp4", ".webm", ".mov", ".gif")) or "/user-attachments/assets/" in u:
            if u not in videos:
                videos.append(u)
        elif low.endswith((".png", ".jpg", ".jpeg", ".webp")) or "githubusercontent" in u:
            if u not in images:
                images.append(u)
    return images, videos


def _page_text(html: str, limit: int = CONTEXT_CHARS):
    soup = BeautifulSoup(html, "lxml")
    meta = {}
    for prop in ("og:image", "twitter:image", "og:description", "description", "og:video", "og:video:url"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            meta[prop] = tag["content"]
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        t.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in root.find_all(["h1", "h2", "h3", "p", "li", "pre"])]
    text = "\n".join(p for p in paras if len(p) > 1)
    return text[:limit], meta


def _github_readme(full: str):
    for name in ("README.md", "readme.md", "README.MD", "Readme.md", "README.rst", "README"):
        try:
            return http_get(f"{RAW}/{full}/HEAD/{name}", retries=1, timeout=30).text, f"{RAW}/{full}/HEAD/{name}"
        except Exception:
            continue
    return "", f"{RAW}/{full}/HEAD/"


_GH_REPO = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/?#].*)?$")
_HF_PAGE = re.compile(r"^https?://huggingface\.co/(spaces/)?([\w.-]+/[\w.-]+)(?:[/?#].*)?$")
_NOT_REPO = {"orgs", "topics", "trending", "collections", "sponsors", "marketplace", "features", "about", "settings", "login"}


def github_repo(url: str):
    """'owner/repo' for a GitHub repository link, else None."""
    m = _GH_REPO.match(url or "")
    return f"{m.group(1)}/{m.group(2)}" if m and m.group(1).lower() not in _NOT_REPO else None


def _from_github(full: str):
    readme, base = _github_readme(full)
    base = base.rsplit("/", 1)[0] + "/"
    images, videos = media_candidates(readme, base)
    images.append(f"https://opengraph.githubassets.com/1/{full}")
    return readme[:CONTEXT_CHARS], images, videos, clip_notes(readme, base, videos)


def _from_hf(page_url: str, space: bool, rid: str):
    base = page_url.rstrip("/") + "/resolve/main/"
    card = http_get(page_url.rstrip("/") + "/raw/main/README.md", retries=1, timeout=30).text
    images, videos = media_candidates(card, base)
    images.append(f"https://cdn-thumbnails.huggingface.co/social-thumbnails/{'spaces/' if space else ''}{rid}.png")
    return re.sub(r"^---.*?---\s*", "", card, flags=re.S)[:CONTEXT_CHARS], images, videos, clip_notes(card, base, videos)


def repo_links(html: str, skip=()) -> list[str]:
    """GitHub repositories a page links to, in order ('owner/repo')."""
    out = []
    for a in BeautifulSoup(html or "", "lxml").select("a[href]"):
        full = github_repo(a["href"])
        if full and full.lower() not in {x.lower() for x in [*skip, *out]}:
            out.append(full)
    return out


def _from_article(page_url: str):
    html = http_get(page_url, retries=1, timeout=30).text
    body, tags = _page_text(html)
    images, videos = [], []
    for key in ("og:image", "twitter:image"):
        if tags.get(key):
            images.append(urljoin(page_url, tags[key]))
    for key in ("og:video", "og:video:url"):
        if tags.get(key) and urlparse(tags[key]).path.lower().endswith((".mp4", ".webm", ".gif")):
            videos.append(urljoin(page_url, tags[key]))
    more, clips = media_candidates(html, page_url)
    images += [u for u in more if u not in images][:6]
    videos += [u for u in clips if u not in videos][:2]
    return body, images, videos, clip_notes(html, page_url, videos), repo_links(html)


def read_source(url: str):
    """(text, images, videos, clip notes, linked repos) of whatever a link points
    to: a GitHub repository is read through its README, a Hugging Face
    model/Space through its card, anything else as an article page (whose
    links to GitHub repositories are returned too)."""
    full = github_repo(url)
    if full:
        return (*_from_github(full), [])
    m = _HF_PAGE.match(url or "")
    if m and m.group(2).split("/")[0] not in ("docs", "blog", "papers", "datasets", "learn"):
        return (*_from_hf(url.split("?")[0].split("#")[0], bool(m.group(1)), m.group(2)), [])
    return _from_article(url)


def geeknews_origin(html: str):
    """(original link, the poster's Korean summary) of a GeekNews topic page. The
    original is the title link at the top of the page; GeekNews itself only has
    a short summary, no pictures or clips."""
    soup = BeautifulSoup(html, "lxml")
    origin = None
    for sel in (".topictitle a[href]", ".topic_title a[href]", "div.topictitle > a", "h1 a[href]", "a.ud[href]"):
        el = soup.select_one(sel)
        if el and el.get("href", "").startswith("http") and "news.hada.io" not in el["href"]:
            origin = el["href"]
            break
    body = soup.select_one("#topic_contents") or soup.select_one(".topic_contents") or soup.select_one("#topic_content")
    if body is not None:  # links the poster put in the summary, e.g. the project's repository
        for a in body.select("a[href^='http']"):
            a.append(f" ({a['href']})")
    if not origin:  # the first outside link above the summary
        stop = body.sourceline if body is not None and body.sourceline else None
        for a in soup.select("a[href^='http']"):
            if "hada.io" in a["href"] or (stop and a.sourceline and a.sourceline > stop):
                continue
            origin = a["href"]
            break
    return origin, body.get_text("\n", strip=True) if body is not None else ""


def fetch_context(item: dict) -> dict:
    """{text, images, videos, clip_notes} for writing the post: what the project or
    article says, the pictures/clips it shows (best first) and the text next to
    each clip (to judge whether it shows what the post is about).

    A news item (GeekNews, Hacker News) only introduces something, so everything
    behind it is read too: the original it points to (the GeekNews topic page
    names it at the top; item["url"] becomes that original), and, when that
    original is an article about a project, the project's GitHub repository
    (README, pictures, demo clips) - from the article's links or the GeekNews
    summary's. The writer gets the news summary and all the originals together."""
    src, meta = item["source"], item.get("meta") or {}
    pieces, images, videos, notes = [], [], [], {}

    def add(label, got):
        body, imgs, clips, clip_notes_ = got[:4]
        if body:
            pieces.append(f"{label}:\n{body}")
        images.extend(u for u in imgs if u not in images)
        videos.extend(u for u in clips if u not in videos)
        notes.update(clip_notes_)
        return got[4] if len(got) > 4 else []

    try:
        if src == "github":
            add(f"GitHub 저장소 README ({item['title']})", _from_github(item["title"]))
        elif src == "hf":
            add(f"Hugging Face 모델 카드 ({item['title']})", _from_hf(item["url"], meta.get("kind") == "space", item["title"]))
        else:
            name = SOURCES.get(src, {}).get("name", src)
            if item.get("summary"):
                pieces.append(f"{name} 목록의 소개:\n{item['summary']}")
            linked = []
            if src == "geeknews" and meta.get("discussion"):
                topic_html = http_get(meta["discussion"], retries=1, timeout=30).text
                origin, summary = geeknews_origin(topic_html)
                if summary:
                    pieces.append(f"GeekNews 요약 (원문을 소개한 글):\n{summary}")
                    body = BeautifulSoup(topic_html, "lxml").select_one("#topic_contents, .topic_contents, #topic_content")
                    linked += repo_links(body.decode() if body is not None else "")
                if origin:
                    item["url"] = origin
                    meta["origin"] = origin
            page_url = item.get("url") or ""
            if page_url.startswith("http") and "news.ycombinator.com" not in page_url and "news.hada.io" not in page_url:
                try:
                    linked += add(f"원문 ({page_url})", read_source(page_url))
                except Exception as exc:
                    log.warning("original %s of %s failed: %s", page_url, item["uid"], exc)
            # the project behind an article: its repository has the pictures and demos
            here = github_repo(page_url)
            repo = next((r for r in linked if not here or r.lower() != here.lower()), None)
            if repo and not here:
                try:
                    add(f"원문이 소개하는 GitHub 저장소 README ({repo})", _from_github(repo))
                    meta["repo"] = f"https://github.com/{repo}"
                except Exception as exc:
                    log.warning("repository %s of %s failed: %s", repo, item["uid"], exc)
    except Exception as exc:  # write from what the listing said
        log.warning("context for %s failed: %s", item["uid"], exc)
    text = "\n\n".join(pieces)[:TOTAL_CHARS] or (item.get("summary") or "")
    return {"text": text, "images": images[:8], "videos": videos[:3], "clip_notes": {u: notes.get(u, "") for u in videos[:3]}}
