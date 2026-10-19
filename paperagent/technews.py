"""TECH card news: trending projects and tech news, explained in Korean.

collect (GitHub Trending, Hacker News, GeekNews, Hugging Face)
  -> screen with the cheap model (is it interesting and explainable?)
  -> for the best few: read the README / article, download a picture and a
     demo clip if there is one
  -> write the explainer and card copy -> render the cards (one is a video)
"""
import io
import logging
import traceback
from pathlib import Path

import anthropic

from . import agent, cardnews, db, storage, video
from .collectors import http_get
from .collectors import tech as sources

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 25 * 1024 * 1024


def collect_and_screen(settings, say=log.info, meter=None) -> dict:
    """New items in, rated, and those below the bar set aside. AI errors propagate."""
    items = sources.collect(settings["tech_sources"], say=say)
    new = sum(1 for it in items if db.insert_tech(it))
    say(f"tech: {new} new of {len(items)} items")
    pending = db.tech_by_status("new")
    if pending:
        scores = agent.screen_tech(pending, meter=meter)
        bar = settings["tech_min_interest"]
        for it in pending:
            interest, reason = scores.get(it["id"], (None, None))
            status = "screened" if interest is not None and interest >= bar else "skipped"
            db.update_tech(it["id"], interest=interest, interest_reason=reason, status=status)
        say(f"tech: rated {len(pending)}, {sum(1 for it in pending if (scores.get(it['id']) or (0,))[0] >= bar)} at interest >= {bar}")
    return {"tech_new": new}


def _download(url: str, limit: int):
    resp = http_get(url, retries=1, timeout=40, stream=True, headers=sources.BROWSER_HEADERS)
    data = bytearray()
    for chunk in resp.iter_content(64 * 1024):
        data += chunk
        if len(data) > limit:
            raise ValueError("too large")
    return bytes(data), (resp.headers.get("Content-Type") or "").split(";")[0].lower()


def _good_image(data: bytes):
    """PNG bytes of a picture big enough for a card, or None."""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(data))
        im.seek(0)
        w, h = im.size
        if w < 480 or h < 240 or w / h > 3.2 or h / w > 2.0:
            return None
        im = im.convert("RGB")
        if w > 1600:
            im = im.resize((1600, round(h * 1600 / w)))
        out = io.BytesIO()
        im.save(out, "PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return None


def fetch_media(item_id: int, context: dict, say=log.info) -> dict:
    """Store up to two pictures and one demo clip. Returns {"images": [keys], "video": key|None, ...urls}."""
    out = {"images": [], "image_urls": [], "video": None, "video_url": None}
    storage.delete_prefix(f"tech/{item_id}/media/")  # read again: no leftovers from the last time
    for url in context.get("images", []):
        if len(out["images"]) >= 2:
            break
        try:
            data, _ = _download(url, MAX_IMAGE_BYTES)
        except Exception:
            continue
        png = _good_image(data)
        if png:
            key = f"tech/{item_id}/media/image{len(out['images']) + 1}.png"
            storage.put(key, png, "image/png")
            out["images"].append(key)
            out["image_urls"].append(url)
    if video.available():
        for url in context.get("videos", []):
            try:
                data, ctype = _download(url, MAX_VIDEO_BYTES)
            except Exception:
                continue
            ext = ".gif" if (ctype == "image/gif" or data[:4] == b"GIF8") else ".mp4" if ("mp4" in ctype or data[4:8] == b"ftyp") \
                else ".webm" if ("webm" in ctype or data[:4] == b"\x1a\x45\xdf\xa3") else ".mov" if "quicktime" in ctype else None
            if not ext:
                continue
            key = f"tech/{item_id}/media/demo{ext}"
            storage.put(key, data)
            out["video"], out["video_url"] = key, url
            out["video_note"] = (context.get("clip_notes") or {}).get(url, "")
            break
    say(f"  media: {len(out['images'])} picture(s){', a demo clip' if out['video'] else ''}")
    return out


MIN_MATERIAL_CHARS = 600


def enough_material(context: dict) -> bool:
    """False when a news item points at an original that could not be read and
    nothing else of substance came through (only the listing and readers'
    comments): such a post would say nothing."""
    if context.get("original_chars") is None or context["original_chars"] >= 300:
        return True
    substance = sum(chars for label, chars in context.get("read") or []
                    if not label.startswith(("Hacker News 댓글", "원문 (")))
    return substance >= MIN_MATERIAL_CHARS


def process_item(item: dict, say=log.info) -> tuple[bool, float]:
    """Write and render one TECH post. Returns (ok, cost_usd)."""
    iid = item["id"]
    stage = getattr(say, "stage", lambda *a: None)
    say(f"tech #{iid}: {item['title'][:90]}")
    meter = agent.CostMeter()
    try:
        stage("download")
        context = sources.fetch_context(item)
        read, failed = context.get("read") or [], context.get("failed") or []
        say("  read: " + (", ".join(f"{label} {chars:,}자" for label, chars in read) or "the listing only"))
        for label, why in failed:
            say(f"  could not read {label}: {why}")
        if not enough_material(context):
            raise ValueError("원문을 읽지 못해 쓸 내용이 없습니다 ("
                             + ("; ".join(f"{label}: {why}" for label, why in failed) or "원문에서 본문을 찾지 못함")
                             + "). 뉴스 목록의 제목만으로는 카드뉴스를 만들지 않습니다. 잠시 뒤 '다시 읽기'를 눌러 주세요.")
        if context.get("original_chars") is not None and context["original_chars"] < 300:
            say("  the original could not be read: writing from the GeekNews summary (written from the original)")
        saved = db.get_tech(iid)
        if item.get("url") != saved.get("url") or item.get("meta") != saved.get("meta"):  # the original / repository found
            db.update_tech(iid, url=item["url"], meta=item.get("meta"))
        media = fetch_media(iid, context, say)
        if media.get("video"):
            context["clip"] = {"url": media["video_url"], "near": media.get("video_note", "")}
        review = agent.write_tech(item, context, meter=meter)
        if media.get("video"):
            say("  demo clip " + ("goes on the cards" if (review.get("demo") or {}).get("use_clip") else "left out (not what the post shows)"))
        review["_cost"] = meter.summary()
        review["_read"] = [{"label": label, "chars": chars} for label, chars in read]
        review["_unread"] = [{"label": label, "why": why} for label, why in failed]
        db.update_tech(iid, media=media)
        item = db.get_tech(iid)
        cards, videos = [], {}
        try:
            stage("render")
            cards, videos = cardnews.render_tech_and_store(item, review)
            say(f"  rendered {len(cards)} cards" + (f" + {len(videos)} video" if videos else ""))
        except Exception as exc:
            say(f"  card rendering failed: {exc}")
        db.update_tech(iid, status="done", review=review, card_paths=cards, card_videos=videos, priority=0,
                       cost_usd=meter.total_usd, reviewed_at=db.now_iso(), error=None)
        say(f"  written · ${meter.total_usd:.3f}")
        return True, meter.total_usd
    except agent.ApiUnavailable:
        raise
    except (agent.ReviewError, anthropic.APIError, ValueError) as exc:
        say(f"  tech post failed: {exc}")
        db.update_tech(iid, status="error", error=str(exc)[:1000], cost_usd=meter.total_usd)
        return False, meter.total_usd
    except Exception as exc:
        if exc.__class__.__name__ == "RunCancelled":
            db.update_tech(iid, status="screened")
            raise
        say(f"  unexpected error: {exc}")
        db.update_tech(iid, status="error", error=traceback.format_exc()[-2000:], cost_usd=meter.total_usd)
        return False, meter.total_usd


def rerender(item_id: int):
    item = db.get_tech(item_id)
    cards, videos = cardnews.render_tech_and_store(item, item["review"])
    db.update_tech(item_id, card_paths=cards, card_videos=videos)
    return cards


def run_stage(settings, say=log.info, meter=None, item_ids=None) -> dict:
    """The TECH part of a run. Returns stats to add to the run's."""
    stats = {"tech_new": 0, "tech_done": 0, "tech_failed": 0, "tech_cost_usd": 0.0}
    if item_ids:
        targets = [t for t in (db.get_tech(i) for i in item_ids) if t]
    else:
        stats.update(collect_and_screen(settings, say, meter))
        targets = db.tech_next_targets(settings["tech_per_run"])
    update = getattr(say, "update", lambda **kw: None)
    done, failed = [], []
    update(phase="tech", stage="테크 소식 작성 중", tech_targets=[{"id": t["id"], "title": t["title"]} for t in targets],
           tech_done=done, tech_failed=failed, tech_current=None)
    for t in targets:
        update(tech_current=t["id"])
        ok, cost = process_item(t, say)
        (done if ok else failed).append(t["id"])
        stats["tech_done" if ok else "tech_failed"] += 1
        stats["tech_cost_usd"] += cost
        update(tech_done=done, tech_failed=failed, tech_current=None)
    return stats


def build_caption(item, review) -> str:
    text = review.get("caption") or review.get("summary") or review.get("one_liner") or ""
    card_like = {"caption": text, "hashtags": review.get("hashtags")}
    body = cardnews._HASHTAG.sub("", text.replace("\\n", "\n")).strip()
    src = sources.SOURCES.get(item["source"], {}).get("name", item["source"])
    return (f"{body}\n\n🔗 {item.get('url') or ''}\n📡 출처: {src}\n\n"
            + " ".join(cardnews.hashtags(card_like, base=cardnews.TECH_BASE_HASHTAGS)))


def demo_seed(render_cards=True):
    """A fictional TECH post for trying the dashboard offline (uid tech demo)."""
    from . import demo

    return demo.seed_tech(render_cards)


def materialize_media(item, tmp: Path) -> dict:
    """Local copies of the stored media for rendering."""
    media = item.get("media") or {}
    out = {"images": [], "video": None}
    for key in media.get("images") or []:
        p = storage.materialize(key, tmp)
        if p:
            out["images"].append(str(p))
    if media.get("video"):
        p = storage.materialize(media["video"], tmp)
        if p:
            out["video"] = str(p)
    return out
