"""Posting a paper's card news to Instagram as one carousel, with its caption.

Uses the Instagram Graph API content publishing flow:
  1. one container per card (image_url + is_carousel_item)
  2. a CAROUSEL container listing them, with the caption
  3. wait until it is FINISHED, then media_publish
  4. read back the permalink

Needs an Instagram professional (business/creator) account connected to a Meta
app, and an access token allowed to publish (INSTAGRAM_ACCESS_TOKEN,
INSTAGRAM_USER_ID). Instagram downloads every image itself, so the cards must
be reachable at a public URL (PUBLIC_BASE_URL, e.g. the Heroku app address).
"""
import logging
import threading
import time
from urllib.parse import urlparse

import requests

from . import cardnews, config, db

log = logging.getLogger(__name__)
MAX_CAROUSEL = 10


class InstagramError(RuntimeError):
    pass


def _token():
    # a refreshed token (see refresh_token) is kept in the database
    return db.get_setting("instagram_token") or config.INSTAGRAM_ACCESS_TOKEN


def configured() -> bool:
    return bool(config.INSTAGRAM_USER_ID and _token())


def _url(path):
    return f"https://{config.INSTAGRAM_GRAPH_HOST}/{config.INSTAGRAM_API_VERSION}/{path.lstrip('/')}"


def _call(method, path, **params):
    params["access_token"] = _token()
    try:
        resp = requests.request(method, _url(path), params=params if method == "GET" else None,
                                data=params if method != "GET" else None, timeout=60)
    except requests.RequestException as exc:
        raise InstagramError(f"인스타그램에 연결하지 못했습니다: {exc}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or "error" in body:
        err = body.get("error") or {}
        raise InstagramError(err.get("error_user_msg") or err.get("message") or f"HTTP {resp.status_code}")
    return body


def public_base(fallback: str = "") -> str:
    """Where Instagram can download the cards from. A local address does not work."""
    base = (config.PUBLIC_BASE_URL or fallback or "").rstrip("/")
    host = urlparse(base).hostname or ""
    if not base.startswith("https://") or host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local"):
        raise InstagramError("인스타그램이 카드 이미지를 내려받을 수 있는 공개 https 주소가 필요합니다. "
                             "배포된 서버(예: Heroku)에서 올리거나 PUBLIC_BASE_URL을 설정해 주세요.")
    return base


def account():
    """{username, ...} of the connected account (also checks the token)."""
    return _call("GET", config.INSTAGRAM_USER_ID, fields="username,account_type,media_count")


def refresh_token():
    """Long-lived tokens last 60 days; refreshing gives another 60 (Instagram Login tokens)."""
    body = requests.get(f"https://{config.INSTAGRAM_GRAPH_HOST}/refresh_access_token",
                        params={"grant_type": "ig_refresh_token", "access_token": _token()}, timeout=30).json()
    if "access_token" not in body:
        raise InstagramError((body.get("error") or {}).get("message") or "토큰을 갱신하지 못했습니다")
    db.set_setting("instagram_token", body["access_token"])
    return body.get("expires_in")


def publish(paper: dict, base_url: str, wait_seconds: int = 120) -> dict:
    """Post the paper's cards as a carousel. Returns {media_id, permalink}."""
    if not configured():
        raise InstagramError("인스타그램 계정이 연결되지 않았습니다 (INSTAGRAM_USER_ID, INSTAGRAM_ACCESS_TOKEN).")
    cards = (paper.get("card_paths") or [])[:MAX_CAROUSEL]
    if len(cards) < 2:
        raise InstagramError("카드뉴스가 없습니다. '카드 다시 만들기'를 먼저 해 주세요.")
    base = public_base(base_url)
    caption = cardnews.build_caption(paper, paper["review"])
    children = []
    for n in range(1, len(cards) + 1):
        body = _call("POST", f"{config.INSTAGRAM_USER_ID}/media", is_carousel_item="true",
                     image_url=f"{base}/files/card/{paper['id']}/{n}.jpg")
        children.append(body["id"])
    carousel = _call("POST", f"{config.INSTAGRAM_USER_ID}/media", media_type="CAROUSEL",
                     children=",".join(children), caption=caption)["id"]
    deadline = time.monotonic() + wait_seconds
    while True:  # Instagram processes the images before the post can be published
        status = _call("GET", carousel, fields="status_code").get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError(f"인스타그램이 이미지를 처리하지 못했습니다 ({status})")
        if time.monotonic() > deadline:
            raise InstagramError("인스타그램 처리 시간이 너무 오래 걸립니다. 잠시 뒤 다시 시도해 주세요.")
        time.sleep(3)
    media_id = _call("POST", f"{config.INSTAGRAM_USER_ID}/media_publish", creation_id=carousel)["id"]
    try:
        permalink = _call("GET", media_id, fields="permalink").get("permalink")
    except InstagramError:
        permalink = None
    return {"media_id": media_id, "permalink": permalink}


def publish_in_background(paper_id: int, base_url: str) -> bool:
    """Start posting; progress is kept on the paper (instagram_status)."""
    paper = db.get_paper(paper_id)
    if not paper or paper.get("instagram_status") == "publishing":
        return False
    db.update_paper(paper_id, instagram_status="publishing", instagram_error=None)

    def work():
        try:
            result = publish(db.get_paper(paper_id), base_url)
            db.update_paper(paper_id, instagram_status="posted", instagram_media_id=result["media_id"],
                            instagram_permalink=result["permalink"])
            db.set_instagram_posted(paper_id, True)
        except Exception as exc:  # shown on the paper page
            log.warning("instagram publish failed for #%s: %s", paper_id, exc)
            db.update_paper(paper_id, instagram_status="error", instagram_error=str(exc)[:500])

    threading.Thread(target=work, daemon=True).start()
    return True
