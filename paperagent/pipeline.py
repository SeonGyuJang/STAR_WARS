"""The repeating job: collect -> screen -> read & review -> cards -> trending."""
import hashlib
import logging
import tempfile
import threading
import traceback
from pathlib import Path

import anthropic

from . import agent, cardnews, config, db, pdfs, storage, technews
from .collectors import arxiv, match_keywords, openalex, short_error, trending, venues

log = logging.getLogger(__name__)
_start_lock = threading.Lock()  # makes "nothing running? then start" atomic within this process

STAGE_LABELS = {"screen": "관련도 판단", "read": "원문 읽는 중", "judge": "평가 중", "write": "리뷰 작성 중",
                "card": "카드 문구 작성 중", "download": "PDF 받는 중", "render": "카드뉴스 그리는 중"}


class RunCancelled(Exception):
    """The admin stopped the run from the dashboard."""


class _SkipPapers(Exception):
    """A TECH-only run: go straight to the TECH stage."""


def is_running() -> bool:
    """True while a run is going here or in another process/dyno."""
    return db.active_run() is not None


def is_due(settings=None) -> bool:
    """Whether interval_hours have passed since the last successful run.
    Lets an hourly Heroku Scheduler job honour the dashboard's interval."""
    settings = settings or db.pipeline_settings()
    last = db.last_finished_run()
    if not last:
        return True
    return last["started_at"] <= db.ago_iso(hours=settings["interval_hours"], minutes=-10)


class RunLogger:
    """Writes the run log and live progress, and is where a stopped run notices:
    every log line checks whether the admin pressed 강제 종료."""

    def __init__(self, run_id):
        self.run_id = run_id
        self.progress = {"phase": "start"}

    def __call__(self, msg):
        log.info(msg)
        self.check()
        db.append_run_log(self.run_id, msg)

    def check(self):
        if db.run_status(self.run_id) == "aborted":
            raise RunCancelled()

    def update(self, **fields):
        self.progress.update(fields)
        db.set_run_progress(self.run_id, self.progress)

    def stage(self, stage, model=None):
        self.check()
        self.update(stage=STAGE_LABELS.get(stage, stage), model=model)


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
            say(f"journal lookup failed for {src['name']}: {short_error(exc)}")
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
    cats = db.list_sources(kind="arxiv", enabled_only=True)
    per_cat = {src["id"]: [] for src in cats}
    if cats:
        try:  # one request for every category (arXiv limits how often a server may ask)
            found = arxiv.fetch_categories([c["value"] for c in cats], keywords, settings["lookback_days"], settings["max_per_source"])
            for p in found:  # each paper goes to the first configured category it is in (its main one first)
                order = [p.get("primary_category")] + list(p.get("categories") or [])
                src = next((c for name in order for c in cats if c["value"] == name), cats[0])
                if len(per_cat[src["id"]]) < settings["max_per_source"]:
                    per_cat[src["id"]].append(p)
            say(f"arXiv: {len(found)} matched in {len(cats)} categories (one request)")
        except Exception as exc:
            say(f"arXiv API unavailable ({short_error(exc)}); using the daily RSS feeds instead")
            for src in cats:
                try:
                    per_cat[src["id"]] = arxiv.fetch_rss(src["value"], keywords, settings["lookback_days"])[: settings["max_per_source"]]
                except Exception as rss_exc:
                    say(f"arXiv {src['value']} RSS failed: {short_error(rss_exc)}")
    for src in cats:
        found = per_cat[src["id"]]
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
                notes.append(f"OpenAlex failed: {short_error(exc)}")
        if meta["arxiv_terms"]:
            try:
                hits = venues.fetch_arxiv(src["name"], meta["arxiv_terms"], keywords, days, settings["max_per_source"])
                found += hits
                notes.append(f"arXiv {len(hits)}")
            except Exception as exc:
                notes.append(f"arXiv failed: {short_error(exc)}")
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
            ok = rel >= settings["min_relevance"] or (p.get("priority") or 0) > 0  # hand-picked/uploaded: read anyway
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
    stage = getattr(say, "stage", lambda *a: None)
    say(f"reading #{pid}: {paper['title'][:90]}")
    db.update_paper(pid, status="reading", error=None)
    try:
        with tempfile.TemporaryDirectory(prefix="paperagent-") as tmp:
            tmp = Path(tmp)
            pdf_path, figures, page1_key = None, [], None
            try:
                stage("download")
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

        card_paths, card_videos = [], {}
        try:
            stage("render")
            card_paths, card_videos = cardnews.render_and_store({**paper, "id": pid}, review)
            say(f"  rendered {len(card_paths)} cards" + (f" + {len(card_videos)} video" if card_videos else ""))
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
            card_videos=card_videos,
            cost_usd=cost,
            priority=0,
            reviewed_at=db.now_iso(),
        )
        if (paper.get("pdf_url") or "").startswith("storage:"):  # uploaded PDF: read, so no longer kept
            storage.delete(paper["pdf_url"][len("storage:"):])
            db.update_paper(pid, pdf_url=None)
        return True, cost
    except (agent.ApiUnavailable, RunCancelled):
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
    keys, videos = cardnews.render_and_store(paper, paper["review"])
    db.update_paper(paper_id, card_paths=keys, card_videos=videos)
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


def next_targets(settings=None):
    """The papers the next run will read, in order: every hand-picked paper, then
    the best `max_full_reads` at or above the minimum relevance. Returns
    (targets, number hand-picked, number eligible)."""
    settings = settings or db.pipeline_settings()
    min_rel = settings["min_relevance"]
    picked = db.papers_by_status("screened", limit=1000, order="relevance", priority=True)
    auto = [p for p in db.papers_by_status("screened", limit=1000, order="relevance", priority=False)
            if (p["relevance"] or 0) >= min_rel]
    return picked + auto[:settings["max_full_reads"]], len(picked), len(auto)


def add_uploaded_pdf(data: bytes, filename: str = "", title: str = "") -> tuple[int, bool]:
    """Add a PDF the admin uploaded. It is kept in storage only until its review
    succeeds. Returns (paper id, whether it is new)."""
    if not data.startswith(b"%PDF"):
        raise ValueError(f"{filename or '파일'}: PDF 파일이 아닙니다")
    if len(data) > pdfs.MAX_PDF_BYTES:
        raise ValueError(f"{filename or '파일'}: 60MB보다 큽니다")
    digest = hashlib.sha256(data).hexdigest()[:20]
    uid = f"upload:{digest}"
    existing = db.get_paper_by_uid(uid)
    if existing:
        return existing["id"], False
    with tempfile.TemporaryDirectory(prefix="upload-") as tmp:
        path = Path(tmp) / "upload.pdf"
        path.write_bytes(data)
        try:
            meta = pdfs.read_metadata(path)
        except Exception as exc:
            raise ValueError(f"{filename or '파일'}: PDF를 열 수 없습니다 ({exc})") from exc
    key = f"uploads/{digest}.pdf"
    storage.put(key, data, "application/pdf")
    keywords = [k["term"] for k in db.list_keywords()]
    name = title.strip() or meta["title"] or Path(filename or "uploaded.pdf").stem
    db.insert_paper({
        "uid": uid, "source_kind": "upload", "source_name": "직접 업로드", "title": name,
        "abstract": meta["abstract"], "authors": meta["authors"], "doi": meta["doi"],
        "url": f"https://arxiv.org/abs/{meta['arxiv_id']}" if meta["arxiv_id"] else (f"https://doi.org/{meta['doi']}" if meta["doi"] else None),
        "pdf_url": f"storage:{key}",
        "matched_keywords": match_keywords(f"{name} {meta['abstract']}", keywords),
    })
    pid = db.get_paper_by_uid(uid)["id"]
    db.update_paper(pid, priority=1)  # read it whatever its relevance
    return pid, True


def run(trigger: str = "schedule", paper_ids=None, tech_only=False, tech_ids=None):
    """One full cycle. Returns the run id, or None if a run is already in progress.

    Collecting papers and refreshing the trend panels need no Claude API, so
    they always run. If the API is unusable (no key, bad key, no credit...),
    only the AI steps stop; the run is marked 'partial' and the dashboard shows
    a notice asking the admin to check the API. The admin can stop a run at any
    time from the dashboard (강제 종료).

    After the papers, the run also makes TECH posts from trending projects and
    tech news (technews.py). tech_only=True runs just that part."""
    with _start_lock:
        if db.active_run() is not None:  # running here or in another dyno/process
            return None
        run_id = db.start_run(trigger)
    say = RunLogger(run_id)
    agent.set_call_listener(say.stage)
    stats = {"new": 0, "screened_in": 0, "reviewed": 0, "failed": 0, "cost_usd": 0.0}
    meter = agent.CostMeter()
    status = "ok"
    keywords = []
    cancelled = False
    try:
        settings = db.pipeline_settings()
        keywords = [k["term"] for k in db.list_keywords()]
        if not keywords:
            say("no keywords registered - add some on the settings page")
        if tech_only:
            raise _SkipPapers()
        if not paper_ids:
            say.update(phase="collect", stage="논문 수집 중")
            resolve_journals(say)
            stats["new"] = collect(keywords, settings, say)
        try:
            agent.ensure_api()
            if paper_ids:
                targets = [p for p in (db.get_paper(i) for i in paper_ids) if p]
                unrated = [p for p in targets if p.get("relevance") is None]
                if unrated:  # e.g. uploaded PDFs: rate them too, then read them anyway
                    say.update(phase="screen", stage="관련도 판단 중")
                    scores = agent.screen(unrated, keywords, meter=meter)
                    for p in unrated:
                        rel, reason = scores.get(p["id"], (None, None))
                        db.update_paper(p["id"], relevance=rel, relevance_reason=reason, status="screened")
                        say(f"relevance of #{p['id']}: {rel} - {reason}")
                    targets = [db.get_paper(p["id"]) for p in targets]
            else:
                min_rel = settings["min_relevance"]
                moved = db.apply_threshold(min_rel)
                if moved["skipped"] or moved["screened"]:
                    say(f"queue matched to relevance {min_rel}: {moved['skipped']} moved to 관련도 미달, "
                        f"{moved['screened']} back to 읽을 차례")
                say.update(phase="screen", stage="관련도 판단 중")
                stats["screened_in"] = screen_new(keywords, settings, say, meter)
                targets, picked, eligible = next_targets(settings)
                say(f"reading {len(targets)} papers: {picked} hand-picked + top {len(targets) - picked} "
                    f"of {eligible} at relevance >= {min_rel}")
            say.update(phase="read", targets=[{"id": p["id"], "title": p["title"]} for p in targets],
                       done=[], failed=[], current=None, stage=None)
            for i, p in enumerate(targets, start=1):
                say.update(current=p["id"], index=i)
                ok, cost = process_paper(p, keywords, say)
                stats["cost_usd"] += cost
                stats["reviewed" if ok else "failed"] += 1
                say.progress["done" if ok else "failed"].append(p["id"])
                say.update(current=None, stage=None)
        except agent.ApiUnavailable as exc:
            status = "partial"
            stats["api_error"] = exc.message
            say(f"AI 단계 중단: {exc.message} API 키와 결제 상태를 확인하세요. 수집과 트렌드는 계속 진행합니다.")
    except _SkipPapers:
        pass
    except RunCancelled:
        cancelled = True
    except Exception as exc:
        status = "error"
        try:
            say(f"run failed: {exc}\n{traceback.format_exc()[-1500:]}")
        except RunCancelled:
            cancelled = True
    finally:
        if not paper_ids and not tech_only and not cancelled:
            try:
                say.update(phase="trending", stage="트렌드 갱신 중", current=None)
                trending.refresh_all(keywords, logger=say)
            except RunCancelled:
                cancelled = True
            except Exception as exc:  # never let trends break the run record
                log.warning("trending failed: %s", exc)
        settings = db.pipeline_settings()
        if not paper_ids and not cancelled and status != "error" and "api_error" not in stats \
                and (tech_only or (settings["tech_sources"] and settings["tech_per_run"] > 0)):
            try:
                say.update(phase="tech", stage="테크 소식 모으는 중", current=None)
                agent.ensure_api()
                tech = technews.run_stage(settings, say, meter, item_ids=tech_ids)
                stats["tech_done"], stats["tech_failed"] = tech["tech_done"], tech["tech_failed"]
                stats["cost_usd"] += tech["tech_cost_usd"]
            except RunCancelled:
                cancelled = True
            except agent.ApiUnavailable as exc:
                status = "partial"
                stats["api_error"] = exc.message
                say(f"테크 소식 단계 중단: {exc.message}")
            except Exception as exc:  # the papers of this run are already saved
                log.warning("tech stage failed: %s", exc)
                try:
                    say(f"tech stage failed: {exc}")
                except RunCancelled:
                    cancelled = True
        agent.set_call_listener(None)
        stats["cost_usd"] = round(stats["cost_usd"] + meter.total_usd, 4)
        if not cancelled:
            try:
                say(f"run cost ${stats['cost_usd']:.3f}")
                say.update(phase="done", stage=None, current=None)
            except RunCancelled:
                cancelled = True
        db.finish_run(run_id, status, stats)
    return run_id


def stop(run_id=None, who="관리자") -> bool:
    """Force-stop the active run (or the given one)."""
    run = db.get_run(run_id) if run_id else db.active_run()
    if not run:
        return False
    return db.abort_run(run["id"], f"{who}가 실행을 강제 종료했습니다. 읽던 논문은 대기열로 돌아갑니다.")


def refresh_trending_only(say=log.info):
    """Trend panels only - no Claude API involved."""
    keywords = [k["term"] for k in db.list_keywords()]
    trending.refresh_all(keywords, logger=say)


def run_in_background(trigger="manual", paper_ids=None, tech_only=False, tech_ids=None) -> bool:
    if is_running():
        return False
    threading.Thread(target=run, kwargs={"trigger": trigger, "paper_ids": paper_ids, "tech_only": tech_only,
                                         "tech_ids": tech_ids}, daemon=True).start()
    return True
