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
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .. import config
from . import http_get, short_error

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
HN_SEARCH = "https://hn.algolia.com/api/v1/search"  # Hacker News search: the best stories of any period
GEEKNEWS_FEEDS = ["https://news.hada.io/rss/news", "https://feeds.feedburner.com/geeknews-feed"]
GEEKNEWS_PAST = "https://news.hada.io/past"  # ?day=YYYY-MM-DD: that day's topics
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
    for row in soup.find_all("article", class_="Box-row"):
        h2 = row.find("h2")
        link = h2.find("a", href=True) if h2 else None
        if not link:
            continue
        full = link["href"].strip("/")
        if full.count("/") != 1:
            continue
        desc = row.find("p")
        lang = row.find(attrs={"itemprop": "programmingLanguage"})
        stars = row.find("a", href=lambda h: bool(h) and h.endswith("/stargazers"))
        forks = row.find("a", href=lambda h: bool(h) and h.endswith("/forks"))
        today = next((s for s in row.find_all("span") if "stars" in s.get_text() and ("today" in s.get_text() or "this week" in s.get_text())), None)
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


def trending_range(days: int) -> str:
    """GitHub Trending only has three ranges: today, this week, this month."""
    return "daily" if days <= 1 else "weekly" if days <= 7 else "monthly"


def github_trending(limit: int = 15, days: int = 1):
    since = trending_range(days)
    try:
        items = parse_github_trending(http_get(GITHUB_TRENDING, params={"since": since}, retries=2).text, limit)
        if items:
            for it in items:
                it["meta"]["range"] = since
            return items
        log.warning("GitHub Trending page had no repository rows; using the search API")
    except Exception as exc:
        log.warning("GitHub Trending page failed (%s); using the search API", exc)
    return github_search(limit, days)


def _gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if config.GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return h


def github_search(limit: int = 15, days: int = 7):
    """Most-starred repositories created in the period (at least a week: a day-old
    repository has few stars yet) - close to 'trending'."""
    since = (date.today() - timedelta(days=max(days, 7))).isoformat()
    data = http_get(f"{GITHUB_API}/search/repositories", params={"q": f"created:>{since}", "sort": "stars", "order": "desc",
                                                                   "per_page": limit}, headers=_gh_headers(), retries=2).json()
    out = []
    for r in data.get("items", [])[:limit]:
        out.append({
            "uid": f"github:{r['full_name'].lower()}", "source": "github", "title": r["full_name"], "url": r["html_url"],
            "summary": r.get("description") or "",
            "meta": {"stars": r.get("stargazers_count"), "forks": r.get("forks_count"), "language": r.get("language"),
                     "topics": r.get("topics", [])[:6], "new_this_week": days <= 7},
        })
    return out


# --- Hacker News -----------------------------------------------------------------

def _hn_item(i, it):
    discussion = f"https://news.ycombinator.com/item?id={i}"
    text = BeautifulSoup(it.get("text") or it.get("story_text") or "", "lxml").get_text(" ", strip=True)
    return {
        "uid": f"hn:{i}", "source": "hn", "title": it.get("title") or "", "url": it.get("url") or discussion,
        "summary": text[:600],
        "meta": {"points": it.get("score", it.get("points")), "comments": it.get("descendants", it.get("num_comments")),
                 "author": it.get("by", it.get("author")), "discussion": discussion,
                 "time": it.get("time", it.get("created_at_i"))},
    }


def hacker_news(limit: int = 15, days: int = 1, min_points: int = 120, scan: int = 40):
    """Today's front page; for a longer period, the period's highest-voted stories
    (Hacker News search), with the front page and best list as a fallback."""
    since = time.time() - days * 86400
    if days > 1:
        try:
            data = http_get(HN_SEARCH, params={"tags": "story", "numericFilters": f"created_at_i>{int(since)},points>={min_points}",
                                               "hitsPerPage": min(max(limit * 3, 30), 200)}, retries=2, timeout=30).json()
            hits = sorted(data.get("hits") or [], key=lambda h: h.get("points") or 0, reverse=True)
            out = [_hn_item(h["objectID"], h) for h in hits if h.get("objectID") and h.get("title")]
            if out:
                return out[:limit]
        except Exception as exc:
            log.warning("Hacker News search failed (%s); using the front page and best stories", short_error(exc))
    ids = http_get(f"{HN_API}/topstories.json", retries=2).json()[:scan]
    if days > 1:  # the best list reaches back a few days
        try:
            ids += [i for i in http_get(f"{HN_API}/beststories.json", retries=1).json()[:scan * 2] if i not in ids]
        except Exception:
            pass
    out = []
    for i in ids:
        try:
            it = http_get(f"{HN_API}/item/{i}.json", retries=2, timeout=20).json() or {}
        except Exception:
            continue
        if it.get("type") != "story" or it.get("dead") or it.get("deleted") or (it.get("score") or 0) < min_points:
            continue
        if days > 1 and (it.get("time") or since) < since:
            continue
        out.append(_hn_item(i, it))
        if len(out) >= limit and days <= 1:
            break
    out.sort(key=lambda x: x["meta"]["points"] or 0, reverse=True)
    return out[:limit]


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
        body_html = BeautifulSoup(body.get_text() if body else "", "lxml")
        summary = body_html.get_text(" ", strip=True)
        # the original, in case the topic page cannot be opened later: an outside <link>, else links in the summary
        links = [link] if _outside(link) else []
        links += [a["href"] for a in body_html.find_all("a", href=True) if _outside(a["href"]) and a["href"] not in links]
        when = e.find("published") or e.find("updated") or e.find("pubDate")
        topic = f"https://news.hada.io/topic?id={m.group(1)}"
        out.append({
            "uid": f"geeknews:{m.group(1)}", "source": "geeknews", "title": title, "url": topic, "summary": summary[:4000],
            "meta": {"discussion": topic, "published": when.get_text(strip=True)[:32] if when else None, "links": links[:5]},
        })
        if len(out) >= limit:
            break
    return out


def _published(text):
    """A feed date (ISO 8601 or RFC 822) as a timestamp, or None."""
    from email.utils import parsedate_to_datetime

    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return parsedate_to_datetime(text).timestamp()
        except (TypeError, ValueError):
            return None


def parse_geeknews_list(html: str, day: str | None = None):
    """Topics on a GeekNews list page (front page, /past?day=...): title, the
    original it links to, the short description and points."""
    soup = BeautifulSoup(html, "lxml")
    rows = soup.find_all(class_="topic_row") or [t.parent for t in soup.find_all(class_="topictitle")]
    out, seen = [], set()
    for row in rows:
        link = row.find("a", href=re.compile(r"topic\?id=(\d+)"))
        m = re.search(r"topic\?id=(\d+)", link["href"]) if link else None
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        head = row.find(class_="topictitle")
        title_el = (head.find("h1") or head) if head else link
        title = title_el.get_text(" ", strip=True)
        origin = head.find("a", href=True) if head else None
        desc = row.find(class_="topicdesc")
        info = row.find(class_="topicinfo")
        points = _int(info.find(id=re.compile(r"^tp")).get_text()) if info and info.find(id=re.compile(r"^tp")) else None
        topic = f"https://news.hada.io/topic?id={m.group(1)}"
        links = [origin["href"]] if origin is not None and _outside(origin["href"]) else []
        if title:
            out.append({"uid": f"geeknews:{m.group(1)}", "source": "geeknews", "title": title, "url": topic,
                        "summary": desc.get_text(" ", strip=True)[:4000] if desc else "",
                        "meta": {"discussion": topic, "published": day, "points": points, "links": links}})
    return out


def geeknews(limit: int = 15, days: int = 1):
    """The feed has the latest topics; a longer period also reads each day's
    list (/past?day=...), best voted first."""
    since = time.time() - days * 86400
    items, last = [], None
    for feed in GEEKNEWS_FEEDS:
        try:
            items = parse_geeknews_feed(http_get(feed, retries=2).text, 100)
            if items:
                break
        except Exception as exc:
            last = exc
    items = [it for it in items if (_published(it["meta"].get("published")) or time.time()) >= since]
    if days > 1:
        found = {it["uid"]: it for it in items}
        for back in range(1, days + 1):
            day = (date.today() - timedelta(days=back)).isoformat()
            try:
                html, _, _ = fetch_page(f"{GEEKNEWS_PAST}?day={day}")
            except Exception as exc:
                log.warning("GeekNews list of %s failed: %s", day, short_error(exc))
                if back == 1:  # closed to us: the feed is all there is
                    break
                continue
            for it in parse_geeknews_list(html, day):
                if it["uid"] not in found:
                    found[it["uid"]] = it
                elif not found[it["uid"]]["meta"].get("points"):
                    found[it["uid"]]["meta"]["points"] = it["meta"]["points"]
        items = sorted(found.values(), key=lambda x: x["meta"].get("points") or 0, reverse=True)
    if not items and last:
        raise last
    return items[:limit]


# --- Hugging Face ----------------------------------------------------------------

def hugging_face(limit: int = 12, days: int = 1):
    """Trending models and Spaces. The trending score already sums up the last
    days' likes; for a period, what has not been touched in it is left out."""
    since = time.time() - max(days, 7) * 86400
    out, errors = [], []
    for kind, path in (("model", "models"), ("space", "spaces")):
        try:
            rows = http_get(f"{HF_API}/{path}", params={"sort": "trendingScore", "direction": -1, "limit": limit * 2}, retries=2).json()
        except Exception as exc:
            log.warning("Hugging Face %s failed: %s", path, short_error(exc))
            errors.append(exc)
            continue
        kept = 0
        for r in rows:
            rid = r.get("id") or r.get("modelId")
            if not rid or kept > limit // 2:
                continue
            touched = _published(r.get("lastModified") or r.get("createdAt"))
            if touched and touched < since:
                continue
            kept += 1
            base = "https://huggingface.co/" + (f"spaces/{rid}" if kind == "space" else rid)
            out.append({
                "uid": f"hf:{kind}/{rid.lower()}", "source": "hf", "title": rid, "url": base,
                "summary": (r.get("cardData") or {}).get("short_description") or r.get("pipeline_tag") or "",
                "meta": {"kind": kind, "likes": r.get("likes"), "downloads": r.get("downloads"),
                         "task": r.get("pipeline_tag"), "sdk": r.get("sdk"), "trending": r.get("trendingScore")},
            })
    if len(errors) == 2:  # both lists failed: report it as a failure, not as "0 items"
        raise errors[-1]
    out.sort(key=lambda x: x["meta"].get("trending") or 0, reverse=True)
    return out[:limit]


COLLECTORS = {"github": github_trending, "hn": hacker_news, "geeknews": geeknews, "hf": hugging_face}


def collect(sources, limit: int = 15, say=log.info, days: int = 1):
    """Items of the last `days` days from the enabled sources; a source that fails
    is reported and skipped."""
    out = []
    for key in sources:
        fn = COLLECTORS.get(key)
        if not fn:
            continue
        try:
            items = fn(limit, days)
            say(f"{SOURCES[key]['name']}: {len(items)} items")
            out.extend(items)
        except Exception as exc:
            say(f"{SOURCES[key]['name']} failed: {short_error(exc)}")
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


_TEXT_TAGS = ["h1", "h2", "h3", "h4", "p", "li", "pre", "blockquote", "figcaption", "td"]


def _content_root(soup):
    """The element holding the article: <article>/<main> when they hold most of
    the page's paragraphs, else the block whose paragraphs add up to the most
    text (many blogs and news sites wrap the story in plain <div>s)."""
    total = sum(len(p.get_text(" ", strip=True)) for p in soup.find_all("p")) or 1
    candidates = (soup.find_all("article") + soup.find_all("main") + soup.find_all(attrs={"role": "main"})
                  + soup.find_all(id="content") + soup.find_all(class_=["post-content", "entry-content", "article-body", "ltx_page_main"]))
    for el in candidates:
        if sum(len(p.get_text(" ", strip=True)) for p in el.find_all("p")) >= total * 0.5:
            return el
    scores = {}
    for p in soup.find_all("p"):
        n = len(p.get_text(" ", strip=True))
        if n < 25:
            continue
        for depth, el in enumerate((p.parent, p.parent.parent if p.parent else None)):
            if el is not None:
                scores[id(el)] = (scores.get(id(el), (0, el))[0] + n / (depth + 1), el)
    if scores:
        return max(scores.values(), key=lambda v: v[0])[1]
    return soup.body or soup


def _page_text(html: str, limit: int = CONTEXT_CHARS):
    soup = BeautifulSoup(html, "lxml")
    meta = {}
    for prop in ("og:image", "twitter:image", "og:description", "description", "og:title", "og:video", "og:video:url"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            meta[prop] = tag["content"]
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg", "iframe"]):
        t.decompose()
    seen, paras = set(), []
    try:
        root = _content_root(soup)
        for el in root.find_all(_TEXT_TAGS):
            if el.find_parent(_TEXT_TAGS) is not None:  # a <p> inside an <li>: counted once
                continue
            t = el.get_text(" ", strip=True)
            if len(t) > 1 and t not in seen:
                seen.add(t)
                paras.append(t)
    except Exception as exc:  # an odd page never costs the article: all its text below
        log.warning("article extraction failed, using the page text: %s", exc)
    text = "\n".join(paras)
    if len(text) < 200:  # no paragraphs to speak of: all the visible text
        body = soup.body or soup
        text = re.sub(r"\n\s*\n+", "\n", body.get_text("\n", strip=True))
    desc = meta.get("og:description") or meta.get("description") or ""
    if desc and desc[:80] not in text:
        text = f"{desc}\n{text}"
    return text[:limit], meta


# Many sites turn away an unknown bot (403) or serve it a stub; an article is
# read the way a browser asks for it.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
}
MIN_ARTICLE_CHARS = 800  # less than this from the HTML: the page is drawn by JavaScript, render it


def _pdf_text(data: bytes, limit: int = CONTEXT_CHARS) -> str:
    import fitz

    with fitz.open(stream=data, filetype="pdf") as doc:
        out = []
        for page in doc:
            out.append(page.get_text())
            if sum(map(len, out)) > limit:
                break
    return re.sub(r"[ \t]+", " ", "\n".join(out)).strip()[:limit]


def render_page(url: str):
    """The page's HTML after its JavaScript has run (headless Chromium), or None."""
    try:
        from playwright.sync_api import sync_playwright

        from .. import cardnews

        with sync_playwright() as p:
            browser = cardnews._launch(p)
            try:
                page = browser.new_page(user_agent=BROWSER_HEADERS["User-Agent"], locale="en-US")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                return page.content()
            finally:
                browser.close()
    except Exception as exc:
        log.warning("rendering %s failed: %s", url, exc)
        return None


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


_GH_IN_TEXT = re.compile(r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+")


def repo_links_text(text: str, skip=()) -> list[str]:
    """GitHub repositories mentioned in Markdown or plain text ('owner/repo')."""
    out = []
    for m in _GH_IN_TEXT.finditer(text or ""):
        full = github_repo(m.group(0).rstrip(".)"))
        if full and full.lower() not in {x.lower() for x in [*skip, *out]}:
            out.append(full)
    return out


def _from_hf(page_url: str, space: bool, rid: str):
    """A model/Space card: text, pictures, clips, and the GitHub repositories it links to (code, demos)."""
    base = page_url.rstrip("/") + "/resolve/main/"
    card = http_get(page_url.rstrip("/") + "/raw/main/README.md", retries=1, timeout=30).text
    images, videos = media_candidates(card, base)
    images.append(f"https://cdn-thumbnails.huggingface.co/social-thumbnails/{'spaces/' if space else ''}{rid}.png")
    body = re.sub(r"^---.*?---\s*", "", card, flags=re.S)[:CONTEXT_CHARS]
    return body, images, videos, clip_notes(card, base, videos), repo_links_text(card)


def repo_links(html: str, skip=()) -> list[str]:
    """GitHub repositories a page links to, in order ('owner/repo')."""
    out = []
    for a in BeautifulSoup(html or "", "lxml").find_all("a", href=True):
        full = github_repo(a["href"])
        if full and full.lower() not in {x.lower() for x in [*skip, *out]}:
            out.append(full)
    return out


def fetch_page(url: str):
    """(html, content type, bytes) of a page, asked for like a browser. A site that
    turns the request away (403/401/429/503, e.g. a bot check) is opened in
    headless Chromium instead."""
    try:
        resp = http_get(url, retries=2, timeout=30, headers=BROWSER_HEADERS)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403, 429, 503):
            html = render_page(url)
            if html and len(BeautifulSoup(html, "lxml").get_text(strip=True)) > 200:
                return html, "text/html", html.encode()
        raise
    return resp.text, (resp.headers.get("Content-Type") or "").lower(), resp.content


_ARXIV = re.compile(r"^https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.I)


def _from_arxiv(aid: str):
    """An arXiv paper: title, authors and abstract from the abstract page, the full
    text and figures from the HTML version (or the PDF when there is none)."""
    head, images, body = [], [], ""
    try:
        html, _, _ = fetch_page(f"https://arxiv.org/abs/{aid}")
        soup = BeautifulSoup(html, "lxml")
        meta = lambda name: [m["content"] for m in soup.find_all("meta", attrs={"name": name}) if m.get("content")]
        title = (meta("citation_title") or [""])[0]
        quote = soup.find("blockquote", class_="abstract")
        abstract = (meta("citation_abstract") or [quote.get_text(" ", strip=True) if quote else ""])[0]
        abstract = re.sub(r"^Abstract:\s*", "", abstract)
        authors = meta("citation_author")
        head = [f"논문 제목: {title}" if title else "", f"저자: {', '.join(authors[:12])}" if authors else "",
                f"초록: {abstract}" if abstract else ""]
    except Exception as exc:
        log.warning("arXiv abstract page %s failed: %s", aid, short_error(exc))
    try:
        resp = http_get(f"https://arxiv.org/html/{aid}", retries=1, timeout=40, headers=BROWSER_HEADERS)
        base = (getattr(resp, "url", None) or f"https://arxiv.org/html/{aid}").rstrip("/") + "/"
        body, _ = _page_text(resp.text)
        figs, _ = media_candidates(resp.text, base)
        images = [u for u in figs if "arxiv.org/html/" in u][:6]
    except Exception:
        try:
            pdf = http_get(f"https://arxiv.org/pdf/{aid}", retries=1, timeout=60, headers=BROWSER_HEADERS).content
            body = _pdf_text(pdf) if pdf[:5] == b"%PDF-" else ""
        except Exception as exc:
            log.warning("arXiv full text %s failed: %s", aid, short_error(exc))
    text = "\n".join(x for x in head if x)
    if body:
        text += ("\n\n본문:\n" if text else "") + body
    if not text:
        raise ValueError(f"arXiv {aid}: neither the abstract nor the paper could be read")
    return text[:CONTEXT_CHARS], images, [], {}, repo_links_text(text)


def _from_article(page_url: str):
    html, ctype, data = fetch_page(page_url)
    if "pdf" in ctype or data[:5] == b"%PDF-":  # papers, reports, slides
        text = _pdf_text(data)
        return text, [], [], {}, repo_links_text(text)
    body, tags = _page_text(html)
    if len(body) < MIN_ARTICLE_CHARS:
        rendered = render_page(page_url)
        if rendered:
            again, more_tags = _page_text(rendered)
            if len(again) > len(body):
                html, body, tags = rendered, again, {**tags, **more_tags}
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


def hn_comments(story_id, limit: int = 8, chars: int = 6000) -> str:
    """The top comments of a Hacker News story: what readers add or question."""
    it = http_get(f"{HN_API}/item/{story_id}.json", retries=1, timeout=20).json() or {}
    out, total = [], 0
    for kid in (it.get("kids") or [])[:limit * 2]:
        try:
            c = http_get(f"{HN_API}/item/{kid}.json", retries=1, timeout=20).json() or {}
        except Exception:
            continue
        if c.get("deleted") or c.get("dead") or not c.get("text"):
            continue
        t = BeautifulSoup(c["text"], "lxml").get_text(" ", strip=True)
        out.append(f"- {t[:900]}")
        total += len(out[-1])
        if len(out) >= limit or total > chars:
            break
    return "\n".join(out)[:chars]


def read_source(url: str):
    """(text, images, videos, clip notes, linked repos) of whatever a link points
    to: a GitHub repository is read through its README, a Hugging Face
    model/Space through its card, anything else as an article page (whose
    links to GitHub repositories are returned too)."""
    full = github_repo(url)
    if full:
        return (*_from_github(full), [])
    m = _ARXIV.match(url or "")
    if m:
        return _from_arxiv(m.group(1))
    m = _HF_PAGE.match(url or "")
    if m and m.group(2).split("/")[0] not in ("docs", "blog", "papers", "datasets", "learn"):
        return _from_hf(url.split("?")[0].split("#")[0], bool(m.group(1)), m.group(2))
    return _from_article(url)


def _geeknews_body(soup):
    return soup.find(id="topic_contents") or soup.find(class_="topic_contents") or soup.find(id="topic_content")


def _outside(href: str) -> bool:
    return (href or "").startswith("http") and "hada.io" not in href


def geeknews_origin(html: str):
    """(original link, the poster's Korean summary) of a GeekNews topic page. The
    original is the title link at the top of the page; GeekNews itself only has
    a short summary, no pictures or clips."""
    soup = BeautifulSoup(html, "lxml")
    origin = None
    heads = soup.find_all(class_=["topictitle", "topic_title"]) + soup.find_all("h1")
    for el in [h.find("a", href=True) for h in heads] + soup.find_all("a", class_="ud", href=True):
        if el is not None and _outside(el.get("href")):
            origin = el["href"]
            break
    body = _geeknews_body(soup)
    if body is not None:  # links the poster put in the summary, e.g. the project's repository
        for a in body.find_all("a", href=re.compile(r"^http")):
            a.append(f" ({a['href']})")
    if not origin:  # the first outside link above the summary
        stop = body.sourceline if body is not None and body.sourceline else None
        for a in soup.find_all("a", href=re.compile(r"^http")):
            if not _outside(a["href"]) or (stop and a.sourceline and a.sourceline > stop):
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
    summary's. The writer gets the news summary and all the originals together.

    Also returns what was read ("read": [(label, chars)]), what could not be
    ("failed": [(label, reason)]) and how much of the original came through
    ("original_chars"), so a post is never written from the news listing alone
    without anyone noticing."""
    item["meta"] = item.get("meta") or {}  # the original and repository found are noted on the item
    src, meta = item["source"], item["meta"]
    pieces, images, videos, notes = [], [], [], {}
    read, failed = [], []
    original = {"chars": None}  # None: the item has no separate original

    def add(label, got):
        body, imgs, clips, clip_notes_ = got[:4]
        if body:
            pieces.append(f"{label}:\n{body}")
            read.append((label, len(body)))
        images.extend(u for u in imgs if u not in images)
        videos.extend(u for u in clips if u not in videos)
        notes.update(clip_notes_)
        return got[4] if len(got) > 4 else []

    try:
        if src == "github":
            add(f"GitHub 저장소 README ({item['title']})", _from_github(item["title"]))
        elif src == "hf":
            linked = add(f"Hugging Face 모델 카드 ({item['title']})", _from_hf(item["url"], meta.get("kind") == "space", item["title"]))
            if linked:  # the model's code repository usually has the demos
                try:
                    add(f"모델 카드가 연결한 GitHub 저장소 README ({linked[0]})", _from_github(linked[0]))
                    meta["repo"] = f"https://github.com/{linked[0]}"
                except Exception as exc:
                    log.warning("repository %s of %s failed: %s", linked[0], item["uid"], exc)
                    failed.append((f"GitHub 저장소 {linked[0]}", short_error(exc)))
        else:
            name = SOURCES.get(src, {}).get("name", src)
            linked = []
            if src == "geeknews":
                original["chars"] = 0  # a GeekNews topic always introduces an original
                summary, origin = "", None
                if meta.get("discussion"):
                    try:
                        topic_html, _, _ = fetch_page(meta["discussion"])
                        origin, summary = geeknews_origin(topic_html)
                        body = _geeknews_body(BeautifulSoup(topic_html, "lxml"))
                        linked += repo_links(body.decode() if body is not None else "")
                    except Exception as exc:
                        log.warning("GeekNews topic of %s failed: %s", item["uid"], short_error(exc))
                        failed.append(("GeekNews 글", short_error(exc)))
                if not summary and item.get("summary"):  # the topic page was closed to us: the feed's copy of the summary
                    summary = item["summary"]
                    linked += repo_links_text(" ".join(meta.get("links") or []))
                if summary:
                    pieces.append(f"GeekNews 요약 (원문을 소개한 글):\n{summary}")
                    read.append(("GeekNews 요약", len(summary)))
                origin = origin or next((u for u in meta.get("links") or [] if not github_repo(u)), None) \
                    or next(iter(meta.get("links") or []), None)
                if origin:
                    item["url"] = origin
                    meta["origin"] = origin
            elif item.get("summary"):
                pieces.append(f"{name} 글 본문:\n{item['summary']}")
                read.append((f"{name} 글 본문", len(item["summary"])))
            page_url = item.get("url") or ""
            if page_url.startswith("http") and "news.ycombinator.com" not in page_url and "hada.io" not in page_url:
                original["chars"] = 0
                try:
                    got = read_source(page_url)
                    original["chars"] = len(got[0] or "")
                    linked += add(f"원문 ({page_url})", got)
                except Exception as exc:
                    log.warning("original %s of %s failed: %s", page_url, item["uid"], exc)
                    failed.append((f"원문 {page_url}", short_error(exc)))
            # the project behind an article: its repository has the pictures and demos
            here = github_repo(page_url)
            repo = next((r for r in linked if not here or r.lower() != here.lower()), None)
            if repo and not here:
                try:
                    add(f"원문이 소개하는 GitHub 저장소 README ({repo})", _from_github(repo))
                    meta["repo"] = f"https://github.com/{repo}"
                except Exception as exc:
                    log.warning("repository %s of %s failed: %s", repo, item["uid"], exc)
                    failed.append((f"GitHub 저장소 {repo}", short_error(exc)))
            if src == "hn" and str(item.get("uid", "")).startswith("hn:"):  # readers' notes, after the original
                try:
                    talk = hn_comments(item["uid"].split(":", 1)[1])
                    if talk:
                        pieces.append(f"Hacker News 댓글 (참고용 독자 반응, 사실은 원문 우선):\n{talk}")
                        read.append(("Hacker News 댓글", len(talk)))
                except Exception as exc:
                    log.warning("comments of %s failed: %s", item["uid"], short_error(exc))
    except Exception as exc:  # write from what the listing said
        log.warning("context for %s failed: %s", item["uid"], short_error(exc))
        failed.append((SOURCES.get(src, {}).get("name", src), short_error(exc)))
    text = "\n\n".join(pieces)[:TOTAL_CHARS] or (item.get("summary") or "")
    return {"text": text, "images": images[:8], "videos": videos[:3], "clip_notes": {u: notes.get(u, "") for u in videos[:3]},
            "read": read, "failed": failed, "original_chars": original["chars"]}
