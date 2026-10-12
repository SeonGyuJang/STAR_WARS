"""The repeating job: collect -> screen -> read & review -> cards -> trending."""
import logging
import tempfile
import threading
import traceback
from pathlib import Path

import anthropic

from . import agent, cardnews, config, db, pdfs, storage
from .collectors import arxiv, openalex, trending, venues

log = logging.getLogger(__name__)
_lock = threading.Lock()


def is_running() -> bool:
    """True while a run is going here or in another process/dyno."""
    return _lock.locked() or db.active_run() is not None


def is_due(settings=None) -> bool:
    """Whether interval_hours have passed since the last successful run.
    Lets an hourly Heroku Scheduler job honour the dashboard's interval."""
    settings = settings or db.pipeline_settings()
    last = db.last_finished_run()
    if not last:
        return True
    return last["started_at"] <= db.ago_iso(hours=settings["interval_hours"], minutes=-10)


class RunLogger:
    def __init__(self, run_id):
        self.run_id = run_id

    def __call__(self, msg):
        log.info(msg)
        db.append_run_log(self.run_id, msg)


def venue_meta(src: dict) -> dict:
    """What we know about a journal/conference source: its type, OpenAlex id and
    the words that name it in arXiv comments."""
    meta = dict(src.get("meta") or {})
    value = src["value"]
    if not meta.get("openalex_id") and value.startswith("S"):
        meta["openalex_id"] = value
    known = venues.find_known(meta.get("full") or src["name"])
    if known:
        meta.setdefault("type", known["type"])
        meta.setdefault("full", known["full"])
        meta.setdefault("arxiv_terms", known["arxiv_terms"])
    meta.setdefault("type", "journal")
    meta.setdefault("arxiv_terms", [])
    return meta


def resolve_journals(say):
    """Fill in sources added by name: link known venues and look up OpenAlex ids.
    A venue that OpenAlex does not know is still collected through arXiv."""
    for src in db.list_sources(kind="journal", enabled_only=True):
        meta = venue_meta(src)
        if meta.get("openalex_id") or meta.get("openalex_checked"):
            if meta != (src.get("meta") or {}):
                db.update_source(src["id"], meta=meta)
            continue
        known = venues.find_known(src["name"])
        try:
            hit = venues.best_openalex_match(known) if known else openalex.resolve_source(src["name"])
        except Exception as exc:
            say(f"journal lookup failed for {src['name']}: {exc}")
            continue
        meta["openalex_checked"] = True
        if hit:
            meta["openalex_id"] = hit["id"]
            meta.setdefault("full", hit["name"])
            say(f"resolved '{src['name']}' -> {hit['name']} ({hit['id']})")
        elif meta["arxiv_terms"]:
            say(f"'{src['name']}' not in OpenAlex; collecting it from arXiv comments only")
        else:
            say(f"journal not found in OpenAlex: {src['name']}")
        db.update_source(src["id"], meta=meta)


def collect(keywords, settings, say):
    new = 0
    for src in db.list_sources(kind="arxiv", enabled_only=True):
        try:
            found = arxiv.fetch_category(src["value"], keywords, settings["lookback_days"], settings["max_per_source"])
        except Exception as exc:
            say(f"arXiv {src['value']} failed: {exc}")
            continue
        added = sum(db.insert_paper({**p, "source_id": src["id"],
                                     "source_name": f"arXiv · {p.get('primary_category') or src['value']}"}) for p in found)
        new += added
        say(f"arXiv {src['value']}: {len(found)} matched, {added} new")
    for src in db.list_sources(kind="journal", enabled_only=True):
        meta = venue_meta(src)
        days = venues.lookback_for(meta["type"], settings["lookback_days"])
        found, notes = [], []
        if meta.get("openalex_id"):
            try:
                hits = venues.fetch_openalex(meta["openalex_id"], keywords, days, settings["max_per_source"])
                found += hits
                notes.append(f"OpenAlex {len(hits)}")
            except Exception as exc:
                notes.append(f"OpenAlex failed: {exc}")
        if meta["arxiv_terms"]:
            try:
                hits = venues.fetch_arxiv(src["name"], meta["arxiv_terms"], keywords, days, settings["max_per_source"])
                found += hits
                notes.append(f"arXiv {len(hits)}")
            except Exception as exc:
                notes.append(f"arXiv failed: {exc}")
        if not notes:
            say(f"{src['name']}: no way to collect yet (not in OpenAlex, no arXiv names)")
            continue
        added = 0
        for p in found:
            if db.insert_paper({**p, "source_kind": "journal", "source_id": src["id"], "source_name": src["name"], "venue": src["name"]}):
                added += 1
            else:
                db.tag_venue(p["uid"], src["name"])
        new += added
        say(f"{src['name']}: {len(found)} matched ({', '.join(notes)}), {added} new")
    return new


def screen_new(keywords, settings, say, meter=None):
    """Rate every not-yet-rated paper. Those under the threshold are kept as
    'skipped' (관련도 미달) so an admin can still pick them by hand."""
    kept = total = 0
    while True:
        pending = db.papers_by_status("new", limit=200)
        if not pending:
            break
        say(f"screening {len(pending)} new papers with {config.SCREEN_MODEL}")
        scores = agent.screen(pending, keywords, meter=meter)
        for p in pending:
            rel, reason = scores.get(p["id"], (0.0, "평가 누락"))
            ok = rel >= settings["min_relevance"]
            kept += ok
            db.update_paper(p["id"], relevance=rel, relevance_reason=reason, status="screened" if ok else "skipped")
        total += len(pending)
    if total:
        say(f"screening done: {kept}/{total} at or above relevance {settings['min_relevance']}")
    return kept


def process_paper(paper: dict, keywords, say=log.info) -> tuple[bool, float]:
    """Fully review one paper and render its cards. Returns
    (ok, cost_usd); a failed review still reports what its finished calls cost.

    The PDF is downloaded into a temporary directory and deleted as soon as
    Claude has read it and the figures are extracted; only the figure PNGs and
    the cards are kept (in storage)."""
    pid = paper["id"]
    before = paper["status"] if paper["status"] != "reading" else "screened"
    db.update_paper(pid, status="reading", error=None)
    say(f"reading #{pid}: {paper['title'][:90]}")
    try:
        with tempfile.TemporaryDirectory(prefix="paperagent-") as tmp:
            tmp = Path(tmp)
            pdf_path, figures, page1_key = None, [], None
            try:
                pdf_path = pdfs.download_pdf(paper, tmp)
                figures = pdfs.extract_figures(pdf_path, tmp / "figs")
                say(f"  downloaded PDF ({pdf_path.stat().st_size // 1024} KB, {len(figures)} figures)")
            except Exception as exc:
                say(f"  no PDF ({exc}); reviewing from abstract")
            review, depth = agent.review(paper, keywords, pdf_path, figures)
            # Keep only the figure images (for the cards and the detail page).
            storage.delete_prefix(f"figures/{pid}/")
            for f in figures:
                f["key"] = storage.put_file(f"figures/{pid}/{Path(f['path']).name}", f.pop("path"))
            if (tmp / "figs" / "page1.png").exists():
                page1_key = storage.put_file(f"figures/{pid}/page1.png", tmp / "figs" / "page1.png")
        # The temporary PDF is gone from here on.
        review["_figures"] = figures
        review["_page1"] = page1_key
        cost = review["_cost"]["total_usd"]
        breakdown = ", ".join("{} {} ${:.3f}".format(c["stage"], c["model"], c["usd"]) for c in review["_cost"]["calls"])
        say(f"  reviewed ({depth}): method {review['methodology_score']} / novelty {review['novelty_score']}"
            f" · ${cost:.3f} ({breakdown})")

        card_paths = []
        try:
            card_paths = cardnews.render_and_store({**paper, "id": pid}, review)
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
            cost_usd=cost,
            priority=0,
            reviewed_at=db.now_iso(),
        )
        return True, cost
    except agent.ApiUnavailable:
        # Not this paper's fault: put it back in the queue and stop the AI steps.
        db.update_paper(pid, status="screened" if before == "skipped" else before)
        raise
    except (agent.ReviewError, anthropic.APIError, ValueError) as exc:
        spent = _spent(exc, say)
        say(f"  review failed: {exc}")
        db.update_paper(pid, status="error", error=str(exc)[:1000], cost_usd=spent)
        return False, spent
    except Exception as exc:
        spent = _spent(exc, say)
        say(f"  unexpected error: {exc}")
        db.update_paper(pid, status="error", error=traceback.format_exc()[-2000:], cost_usd=spent)
        return False, spent


def _spent(exc, say):
    spent = getattr(exc, "cost_usd", 0.0) or 0.0
    if spent:
        say(f"  ${spent:.3f} spent before the failure")
    return spent


def rerender_cards(paper_id: int):
    paper = db.get_paper(paper_id)
    keys = cardnews.render_and_store(paper, paper["review"])
    db.update_paper(paper_id, card_paths=keys)
    return keys


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
    row = db.get_paper_by_uid(p["uid"])
    if row["status"] != "done":
        db.update_paper(row["id"], status="screened", relevance=10.0, relevance_reason="직접 추가한 논문")
    return row["id"]


def run(trigger: str = "schedule", paper_ids=None):
    """One full cycle. Returns the run id, or None if a run is already in progress.

    Collecting papers and refreshing the trend panels need no Claude API, so
    they always run. If the API is unusable (no key, bad key, no credit...),
    only the AI steps stop; the run is marked 'partial' and the dashboard shows
    a notice asking the admin to check the API."""
    if not _lock.acquire(blocking=False):
        return None
    if db.active_run() is not None:  # running in another dyno/process
        _lock.release()
        return None
    run_id = db.start_run(trigger)
    say = RunLogger(run_id)
    stats = {"new": 0, "screened_in": 0, "reviewed": 0, "failed": 0, "cost_usd": 0.0}
    meter = agent.CostMeter()
    status = "ok"
    keywords = []
    try:
        settings = db.pipeline_settings()
        keywords = [k["term"] for k in db.list_keywords()]
        if not keywords:
            say("no keywords registered - add some on the settings page")
        if not paper_ids:
            resolve_journals(say)
            stats["new"] = collect(keywords, settings, say)
        try:
            agent.ensure_api()
            if paper_ids:
                targets = [db.get_paper(i) for i in paper_ids]
            else:
                min_rel = settings["min_relevance"]
                moved = db.apply_threshold(min_rel)
                if moved["skipped"] or moved["screened"]:
                    say(f"queue matched to relevance {min_rel}: {moved['skipped']} moved to 관련도 미달, "
                        f"{moved['screened']} back to 읽을 차례")
                stats["screened_in"] = screen_new(keywords, settings, say, meter)
                # hand-picked papers first (all of them), then the best N at or above the threshold
                targets = db.papers_by_status("screened", limit=1000, order="relevance", priority=True)
                auto = [p for p in db.papers_by_status("screened", limit=1000, order="relevance", priority=False)
                        if (p["relevance"] or 0) >= min_rel]
                picked = len(targets)
                targets += auto[:settings["max_full_reads"]]
                say(f"reading {len(targets)} papers: {picked} hand-picked + top {len(targets) - picked} "
                    f"of {len(auto)} at relevance >= {min_rel}")
            for p in targets:
                if p is None:
                    continue
                ok, cost = process_paper(p, keywords, say)
                stats["cost_usd"] += cost
                stats["reviewed" if ok else "failed"] += 1
        except agent.ApiUnavailable as exc:
            status = "partial"
            stats["api_error"] = exc.message
            say(f"AI 단계 중단: {exc.message} API 키와 결제 상태를 확인하세요. 수집과 트렌드는 계속 진행합니다.")
    except Exception as exc:
        status = "error"
        say(f"run failed: {exc}\n{traceback.format_exc()[-1500:]}")
    finally:
        if not paper_ids:
            try:
                trending.refresh_all(keywords, logger=say)
            except Exception as exc:  # never let trends break the run record
                say(f"trending failed: {exc}")
        stats["cost_usd"] = round(stats["cost_usd"] + meter.total_usd, 4)
        say(f"run cost ${stats['cost_usd']:.3f}")
        db.finish_run(run_id, status, stats)
        _lock.release()
    return run_id


def refresh_trending_only(say=log.info):
    """Trend panels only - no Claude API involved."""
    keywords = [k["term"] for k in db.list_keywords()]
    trending.refresh_all(keywords, logger=say)


def run_in_background(trigger="manual", paper_ids=None) -> bool:
    if is_running():
        return False
    threading.Thread(target=run, kwargs={"trigger": trigger, "paper_ids": paper_ids}, daemon=True).start()
    return True
