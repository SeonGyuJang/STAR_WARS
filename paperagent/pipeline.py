"""The repeating job: collect -> screen -> read & review -> cards -> trending."""
import logging
import threading
import traceback
from pathlib import Path

import anthropic

from . import agent, cardnews, config, db, gdrive, pdfs
from .collectors import arxiv, openalex, trending

log = logging.getLogger(__name__)
_lock = threading.Lock()


def is_running() -> bool:
    return _lock.locked()


class RunLogger:
    def __init__(self, run_id):
        self.run_id = run_id

    def __call__(self, msg):
        log.info(msg)
        db.append_run_log(self.run_id, msg)


def resolve_journals(say):
    for src in db.list_sources(kind="journal", enabled_only=True):
        if not src["value"].startswith("name:"):
            continue
        try:
            hit = openalex.resolve_source(src["name"])
        except Exception as exc:
            say(f"journal lookup failed for {src['name']}: {exc}")
            continue
        if not hit:
            say(f"journal not found in OpenAlex: {src['name']}")
            continue
        try:
            db.update_source(src["id"], value=hit["id"], name=hit["name"])
        except Exception:  # already registered under its id
            db.delete_source(src["id"])
        say(f"resolved journal '{src['name']}' -> {hit['name']} ({hit['id']})")


def collect(keywords, settings, say):
    new = 0
    for src in db.list_sources(kind="arxiv", enabled_only=True):
        try:
            found = arxiv.fetch_category(src["value"], keywords, settings["lookback_days"], settings["max_per_source"])
        except Exception as exc:
            say(f"arXiv {src['value']} failed: {exc}")
            continue
        added = sum(db.insert_paper({**p, "source_name": f"arXiv · {p.get('primary_category') or src['value']}"}) for p in found)
        new += added
        say(f"arXiv {src['value']}: {len(found)} matched, {added} new")
    for src in db.list_sources(kind="journal", enabled_only=True):
        if src["value"].startswith("name:"):
            continue
        try:
            found = openalex.fetch_journal(src["value"], src["name"], keywords, settings["lookback_days"], settings["max_per_source"])
        except Exception as exc:
            say(f"journal {src['name']} failed: {exc}")
            continue
        added = sum(db.insert_paper(p) for p in found)
        new += added
        say(f"{src['name']}: {len(found)} matched, {added} new")
    return new


def screen_new(keywords, settings, say):
    pending = db.papers_by_status("new", limit=200)
    if not pending:
        return 0
    say(f"screening {len(pending)} new papers with {config.SCREEN_MODEL}")
    scores = agent.screen(pending, keywords)
    kept = 0
    for p in pending:
        rel, reason = scores.get(p["id"], (0.0, "평가 누락"))
        ok = rel >= settings["min_relevance"]
        kept += ok
        db.update_paper(p["id"], relevance=rel, relevance_reason=reason, status="screened" if ok else "skipped")
    say(f"screening done: {kept} above relevance {settings['min_relevance']}")
    return kept


def process_paper(paper: dict, keywords, say=log.info) -> bool:
    """Download, store, fully review and render cards for one paper."""
    pid = paper["id"]
    db.update_paper(pid, status="reading", error=None)
    say(f"reading #{pid}: {paper['title'][:90]}")
    try:
        pdf_path, figures = None, []
        try:
            pdf_path = pdfs.download_pdf(paper)
            db.update_paper(pid, local_pdf=str(pdf_path))
            paper["local_pdf"] = str(pdf_path)
            figures = pdfs.extract_figures(pdf_path, config.FIGURE_DIR / str(pid))
            paper["_page1"] = str(config.FIGURE_DIR / str(pid) / "page1.png")
            say(f"  pdf saved as '{pdf_path.name}' ({len(figures)} figures)")
        except Exception as exc:
            say(f"  no PDF ({exc}); reviewing from abstract")

        if pdf_path and gdrive.enabled():
            try:
                link = gdrive.upload_and_share(pdf_path)
                db.update_paper(pid, drive_link=link)
                paper["drive_link"] = link
                say("  uploaded to Google Drive")
            except Exception as exc:
                say(f"  Drive upload failed: {exc}")

        review, depth = agent.review(paper, keywords, pdf_path, figures)
        review["_figures"] = figures
        review["_page1"] = paper.get("_page1")
        say(f"  reviewed ({depth}): method {review['methodology_score']} / novelty {review['novelty_score']}")

        card_paths = []
        try:
            card_paths = cardnews.render(paper, review, figures, config.CARD_DIR / str(pid))
            say(f"  rendered {len(card_paths)} cards")
        except Exception as exc:
            say(f"  card rendering failed: {exc}")

        db.update_paper(
            pid,
            status="done",
            review=review,
            read_depth=depth,
            score_method=review["methodology_score"],
            score_novelty=review["novelty_score"],
            score_overall=review["overall_score"],
            card_paths=card_paths,
        )
        return True
    except (agent.ReviewError, anthropic.APIError, ValueError) as exc:
        say(f"  review failed: {exc}")
        db.update_paper(pid, status="error", error=str(exc)[:1000])
        return False
    except Exception as exc:
        say(f"  unexpected error: {exc}")
        db.update_paper(pid, status="error", error=traceback.format_exc()[-2000:])
        return False


def rerender_cards(paper_id: int):
    paper = db.get_paper(paper_id)
    review = paper["review"]
    figures = [f for f in review.get("_figures", []) if Path(f["path"]).exists()]
    paper["_page1"] = review.get("_page1")
    paths = cardnews.render(paper, review, figures, config.CARD_DIR / str(paper_id))
    db.update_paper(paper_id, card_paths=paths)
    return paths


def add_arxiv_paper(arxiv_id: str):
    """Manually queue a specific arXiv paper (bypasses screening)."""
    found = arxiv.fetch_by_ids([arxiv_id])
    if not found:
        raise ValueError(f"arXiv id not found: {arxiv_id}")
    p = found[0]
    keywords = [k["term"] for k in db.list_keywords()]
    from .collectors import match_keywords

    p["matched_keywords"] = match_keywords(f"{p['title']} {p['abstract']}", keywords)
    p["source_name"] = f"arXiv · {p.get('primary_category') or ''}"
    db.insert_paper(p)
    with db.connect() as conn:
        row = conn.execute("SELECT id, status FROM papers WHERE uid=?", (p["uid"],)).fetchone()
    if row["status"] != "done":
        db.update_paper(row["id"], status="screened", relevance=10.0, relevance_reason="직접 추가한 논문")
    return row["id"]


def run(trigger: str = "schedule", paper_ids=None):
    """One full cycle. Returns the run id, or None if a run is already in progress."""
    if not _lock.acquire(blocking=False):
        return None
    run_id = db.start_run(trigger)
    say = RunLogger(run_id)
    stats = {"new": 0, "screened_in": 0, "reviewed": 0, "failed": 0}
    status = "ok"
    try:
        settings = db.pipeline_settings()
        keywords = [k["term"] for k in db.list_keywords()]
        if not keywords:
            say("no keywords registered - add some on the settings page")
        if paper_ids:
            targets = [db.get_paper(i) for i in paper_ids]
        else:
            resolve_journals(say)
            stats["new"] = collect(keywords, settings, say)
            stats["screened_in"] = screen_new(keywords, settings, say)
            targets = db.papers_by_status("screened", limit=settings["max_full_reads"], order="relevance DESC, created_at DESC")
        for p in targets:
            if p is None:
                continue
            if process_paper(p, keywords, say):
                stats["reviewed"] += 1
            else:
                stats["failed"] += 1
        if not paper_ids:
            trending.refresh_all(keywords, logger=say)
    except Exception as exc:
        status = "error"
        say(f"run failed: {exc}\n{traceback.format_exc()[-1500:]}")
    finally:
        db.finish_run(run_id, status, stats)
        _lock.release()
    return run_id


def run_in_background(trigger="manual", paper_ids=None) -> bool:
    if is_running():
        return False
    threading.Thread(target=run, kwargs={"trigger": trigger, "paper_ids": paper_ids}, daemon=True).start()
    return True
