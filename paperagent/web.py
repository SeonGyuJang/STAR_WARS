"""Dashboard routes."""
import hmac
import io
import re
import secrets
import threading
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, session,
                   url_for)

from . import agent, cardnews, config, db, pipeline, scheduler, storage
from .collectors import openalex

bp = Blueprint("web", __name__)

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
ARXIV_CAT_RE = re.compile(r"^[a-z\-]+(\.[A-Za-z\-]+)?$")


# --- auth & csrf --------------------------------------------------------------

LOOPBACK = {"127.0.0.1", "::1"}


def is_admin():
    """Logged-in admin. Without ADMIN_PASSWORD only requests from this machine
    (127.0.0.1, local testing) count as admin; a public server such as Heroku,
    where requests arrive through a router, then has no admin until a password
    is set."""
    if config.ADMIN_PASSWORD:
        return session.get("admin") is True
    return request.remote_addr in LOOPBACK


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not is_admin():
            flash("관리자 로그인이 필요합니다.", "warn")
            return redirect(url_for("web.login", next=request.path))
        return fn(*a, **kw)

    return wrapper


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]


@bp.before_app_request
def check_csrf():
    if request.method == "POST":
        token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not token or not hmac.compare_digest(token, session.get("_csrf", "")):
            abort(400, "CSRF token missing or invalid")


@bp.app_context_processor
def inject_globals():
    return {
        "site_name": config.SITE_NAME,
        "tagline": config.BRAND_TAGLINE,
        "csrf_token": csrf_token,
        "is_admin": is_admin(),
        "admin_enabled": bool(config.ADMIN_PASSWORD),
        "running": pipeline.is_running(),
        "api": agent.api_status(),
    }


@bp.app_template_filter("short_date")
def short_date(value):
    return (value or "")[:10]


@bp.app_template_filter("kst")
def to_local(value, fmt="%Y-%m-%d %H:%M"):
    """UTC ISO string or datetime -> display timezone (Asia/Seoul by default)."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(config.TIMEZONE).strftime(fmt)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if config.ADMIN_PASSWORD and hmac.compare_digest(request.form.get("password", ""), config.ADMIN_PASSWORD):
            session["admin"] = True
            nxt = request.args.get("next") or ""
            # only allow local paths as redirect targets
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("web.index"))
        flash("비밀번호가 올바르지 않습니다.", "error")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.pop("admin", None)
    return redirect(url_for("web.index"))


# --- dashboard ----------------------------------------------------------------

@bp.route("/")
def index():
    keyword = request.args.get("keyword") or None
    sort = request.args.get("sort", "recent")
    search = request.args.get("q") or None
    days = request.args.get("days", type=int)
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = 24
    papers = db.query_papers(keyword=keyword, search=search, sort=sort, days=days, limit=per_page + 1,
                             offset=(page - 1) * per_page)
    has_more = len(papers) > per_page
    hf, _ = db.get_trending("hf_daily")
    surge, _ = db.get_trending("surge")
    return render_template(
        "index.html",
        papers=papers[:per_page],
        has_more=has_more,
        page=page,
        keywords=db.list_keywords(),
        keyword_counts=db.keyword_counts(),
        counts=db.paper_counts(),
        filters={"keyword": keyword, "sort": sort, "q": search or "", "days": days},
        filter_args={k: v for k, v in {"keyword": keyword, "sort": sort, "q": search, "days": days}.items() if v},
        hf=(hf or [])[:6],
        surge=(surge or [])[:5],
        runs=db.recent_runs(1),
        next_run=scheduler.next_run(),
    )


@bp.route("/paper/<int:paper_id>")
def paper_detail(paper_id):
    p = db.get_paper(paper_id)
    if not p:
        abort(404)
    caption = None
    if p["status"] == "done" and p.get("review"):
        caption = cardnews.build_caption(p, p["review"])
    figures = [f for f in (p.get("review") or {}).get("_figures", []) if f.get("key")]
    return render_template("paper.html", p=p, r=p.get("review") or {}, caption=caption, figures=figures,
                           share_url=request.url)


@bp.route("/trending")
def trending_view():
    panels = {}
    for kind in ("hf_daily", "active", "surge", "tracked"):
        panels[kind] = db.get_trending(kind)
    raw = db.keyword_activity(14)
    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    activity = sorted(
        ({"kw": kw, "series": [(d, per_day.get(d, 0)) for d in days], "total": sum(per_day.values())}
         for kw, per_day in raw.items()),
        key=lambda a: a["total"], reverse=True,
    )
    peak = max((n for a in activity for _, n in a["series"]), default=1) or 1
    return render_template("trending.html", panels=panels, activity=activity, peak=peak)


@bp.route("/queue")
def queue():
    lists = {s: db.papers_by_status(s, limit=300, order="relevance") for s in ("screened", "reading", "error", "skipped", "new")}
    return render_template("queue.html", lists=lists, threshold=db.pipeline_settings()["min_relevance"])


def _api_ready_or_flash() -> bool:
    status = agent.api_status()
    if status.get("ok"):
        return True
    flash(f"Claude API를 쓸 수 없어 AI가 논문을 읽지 못합니다: {status.get('message')} "
          "API 키와 결제 상태를 확인한 뒤 설정 페이지에서 'API 다시 확인'을 눌러 주세요.", "error")
    return False


@bp.route("/queue/read", methods=["POST"])
@admin_required
def queue_read():
    """Hand-pick papers (including ones under the relevance threshold) for the AI to read."""
    ids = [int(i) for i in request.form.getlist("paper_id") if i.isdigit()]
    if not ids:
        flash("읽힐 논문을 하나 이상 선택해 주세요.", "warn")
        return redirect(url_for("web.queue"))
    n = db.queue_papers(ids)
    if request.form.get("when") == "now" and _api_ready_or_flash():
        if pipeline.run_in_background("manual", paper_ids=ids):
            flash(f"선택한 {n}편을 지금 읽기 시작했습니다. 몇 분 뒤 새로고침해 주세요.", "ok")
        else:
            flash(f"다른 실행이 진행 중이라 {n}편을 대기열 맨 앞에 넣었습니다. 다음 실행 때 가장 먼저 읽습니다.", "warn")
    else:
        flash(f"{n}편을 대기열 맨 앞에 넣었습니다. 다음 실행 때 가장 먼저 읽습니다.", "ok")
    return redirect(url_for("web.queue"))


# --- files --------------------------------------------------------------------

def _send_stored(key: str, download_name=None, as_attachment=False):
    item = storage.get(key)
    if item is None:
        abort(404)
    data, content_type = item
    resp = send_file(io.BytesIO(data), mimetype=content_type, download_name=download_name or key.rsplit("/", 1)[-1],
                     as_attachment=as_attachment, max_age=86400)
    return resp


@bp.route("/files/card/<int:paper_id>/<int:n>")
def file_card(paper_id, n):
    return _send_stored(f"cards/{paper_id}/card_{n:02d}.png", f"paper{paper_id}_card_{n:02d}.png",
                        as_attachment=request.args.get("dl") == "1")


@bp.route("/files/cards/<int:paper_id>.zip")
def file_cards_zip(paper_id):
    p = db.get_paper(paper_id)
    if not p or not p.get("card_paths"):
        abort(404)
    data = cardnews.zip_cards(paper_id, p["card_paths"])
    return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True,
                     download_name=f"cardnews-{cardnews.slug(p['title'])}.zip")


@bp.route("/files/figure/<int:paper_id>/<name>")
def file_figure(paper_id, name):
    if not re.fullmatch(r"(fig\d+|page1)\.png", name):
        abort(404)
    return _send_stored(f"figures/{paper_id}/{name}")


# --- settings & admin actions -------------------------------------------------

@bp.route("/settings")
@admin_required
def settings():
    candidates = session.pop("journal_candidates", None)
    return render_template(
        "settings.html",
        keywords=db.list_keywords(),
        sources=db.list_sources(),
        settings=db.pipeline_settings(),
        runs=db.recent_runs(8),
        next_run=scheduler.next_run(),
        candidates=candidates,
        models={"screen": config.SCREEN_MODEL, "read": config.READER_MODEL,
                "judge": config.JUDGE_MODEL, "write": config.WRITER_MODEL},
        cost=db.cost_summary(30),
        on_heroku=config.ON_HEROKU,
        scheduler_enabled=config.SCHEDULER_ENABLED,
        timezone_name=str(config.TIMEZONE),
    )


@bp.route("/settings/api-check", methods=["POST"])
@admin_required
def api_check():
    status = agent.check_api()
    if status.get("ok"):
        flash("Claude API가 정상입니다. 다음 실행부터 AI가 다시 논문을 읽습니다.", "ok")
    else:
        flash(f"Claude API를 쓸 수 없습니다: {status.get('message')}", "error")
    return redirect(request.referrer or url_for("web.settings"))


@bp.route("/trending/refresh", methods=["POST"])
@admin_required
def trending_refresh():
    """Refresh the trend panels (no Claude API needed)."""
    if pipeline.is_running():
        flash("다른 실행이 진행 중입니다. 끝나면 트렌드도 함께 갱신됩니다.", "warn")
    else:
        threading.Thread(target=pipeline.refresh_trending_only, daemon=True).start()
        flash("트렌드를 갱신하고 있습니다. 1~2분 뒤 새로고침해 주세요.", "ok")
    return redirect(url_for("web.trending_view"))


@bp.route("/settings/keywords", methods=["POST"])
@admin_required
def add_keyword():
    added = 0
    for term in re.split(r"[,\n]", request.form.get("term", "")):
        added += db.add_keyword(term[:80])
    flash(f"키워드 {added}개를 추가했습니다." if added else "추가된 키워드가 없습니다 (중복이거나 빈 값).",
          "ok" if added else "warn")
    return redirect(url_for("web.settings") + "#keywords")


@bp.route("/settings/keywords/<int:kid>/delete", methods=["POST"])
@admin_required
def delete_keyword(kid):
    db.delete_keyword(kid)
    flash("키워드를 삭제했습니다.", "ok")
    return redirect(url_for("web.settings") + "#keywords")


@bp.route("/settings/sources/arxiv", methods=["POST"])
@admin_required
def add_arxiv_source():
    cat = request.form.get("category", "").strip()
    if not ARXIV_CAT_RE.match(cat):
        flash("arXiv 카테고리 형식이 아닙니다 (예: cs.RO, stat.ML).", "error")
    elif db.add_source("arxiv", cat, f"arXiv · {cat}"):
        flash(f"arXiv {cat} 카테고리를 추가했습니다.", "ok")
    else:
        flash("이미 등록된 카테고리입니다.", "warn")
    return redirect(url_for("web.settings") + "#sources")


@bp.route("/settings/sources/journal/search", methods=["POST"])
@admin_required
def search_journal():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("web.settings") + "#sources")
    try:
        session["journal_candidates"] = {"query": name, "results": openalex.search_sources(name)}
    except Exception as exc:
        flash(f"OpenAlex 검색 실패: {exc}", "error")
    return redirect(url_for("web.settings") + "#sources")


@bp.route("/settings/sources/journal", methods=["POST"])
@admin_required
def add_journal():
    sid, name = request.form.get("id", "").strip(), request.form.get("name", "").strip()
    if not re.fullmatch(r"S\d+", sid) or not name:
        flash("잘못된 저널 선택입니다.", "error")
    elif db.add_source("journal", sid, name):
        flash(f"저널 '{name}'을(를) 추가했습니다.", "ok")
    else:
        flash("이미 등록된 저널입니다.", "warn")
    return redirect(url_for("web.settings") + "#sources")


@bp.route("/settings/sources/<int:sid>/toggle", methods=["POST"])
@admin_required
def toggle_source(sid):
    src = next((s for s in db.list_sources() if s["id"] == sid), None)
    if src:
        db.update_source(sid, enabled=0 if src["enabled"] else 1)
    return redirect(url_for("web.settings") + "#sources")


@bp.route("/settings/sources/<int:sid>/delete", methods=["POST"])
@admin_required
def delete_source(sid):
    db.delete_source(sid)
    flash("소스를 삭제했습니다.", "ok")
    return redirect(url_for("web.settings") + "#sources")


@bp.route("/settings/pipeline", methods=["POST"])
@admin_required
def save_pipeline():
    f = request.form
    try:
        values = {
            "interval_hours": min(max(int(f["interval_hours"]), 1), 168),
            "lookback_days": min(max(int(f["lookback_days"]), 1), 30),
            "max_per_source": min(max(int(f["max_per_source"]), 5), 200),
            "max_full_reads": min(max(int(f["max_full_reads"]), 0), 50),
            "min_relevance": min(max(float(f["min_relevance"]), 0.0), 10.0),
        }
    except (KeyError, ValueError):
        flash("숫자를 확인해 주세요.", "error")
        return redirect(url_for("web.settings") + "#pipeline")
    for k, v in values.items():
        db.set_setting(k, v)
    flash("파이프라인 설정을 저장했습니다.", "ok")
    return redirect(url_for("web.settings") + "#pipeline")


@bp.route("/run", methods=["POST"])
@admin_required
def run_now():
    api_ok = agent.api_status().get("ok")
    if pipeline.run_in_background("manual"):
        if api_ok:
            flash("파이프라인을 시작했습니다. 진행 상황은 설정 페이지의 실행 기록에서 볼 수 있습니다.", "ok")
        else:
            flash("Claude API에 문제가 있어 논문 수집과 트렌드 갱신만 진행합니다. API 키와 결제 상태를 확인해 주세요.", "warn")
    else:
        flash("이미 실행 중입니다.", "warn")
    return redirect(request.referrer or url_for("web.index"))


@bp.route("/papers/add", methods=["POST"])
@admin_required
def add_paper():
    m = ARXIV_ID_RE.search(request.form.get("arxiv", ""))
    if not m:
        flash("arXiv 링크나 ID를 입력해 주세요 (예: 2410.01234).", "error")
        return redirect(request.referrer or url_for("web.settings"))
    try:
        pid = pipeline.add_arxiv_paper(m.group(1))
    except Exception as exc:
        flash(f"논문을 가져오지 못했습니다: {exc}", "error")
        return redirect(request.referrer or url_for("web.settings"))
    if not _api_ready_or_flash():
        return redirect(url_for("web.paper_detail", paper_id=pid))
    if pipeline.run_in_background("manual", paper_ids=[pid]):
        flash("논문을 읽기 시작했습니다. 몇 분 뒤 새로고침해 주세요.", "ok")
    else:
        flash("다른 실행이 진행 중입니다. 논문은 대기열에 추가되어 다음 실행 때 읽습니다.", "warn")
    return redirect(url_for("web.paper_detail", paper_id=pid))


@bp.route("/paper/<int:paper_id>/reprocess", methods=["POST"])
@admin_required
def reprocess(paper_id):
    if not db.get_paper(paper_id):
        abort(404)
    if not _api_ready_or_flash():
        return redirect(request.referrer or url_for("web.paper_detail", paper_id=paper_id))
    if pipeline.run_in_background("manual", paper_ids=[paper_id]):
        flash("AI가 이 논문을 읽기 시작했습니다. 몇 분 뒤 새로고침해 주세요.", "ok")
    else:
        db.queue_papers([paper_id])
        flash("다른 실행이 진행 중입니다. 대기열 맨 앞에 넣었습니다.", "warn")
    return redirect(url_for("web.paper_detail", paper_id=paper_id))


@bp.route("/paper/<int:paper_id>/rerender", methods=["POST"])
@admin_required
def rerender(paper_id):
    try:
        pipeline.rerender_cards(paper_id)
        flash("카드뉴스를 다시 만들었습니다.", "ok")
    except Exception as exc:
        flash(f"카드뉴스 생성 실패: {exc}", "error")
    return redirect(url_for("web.paper_detail", paper_id=paper_id) + "#cards")


# --- JSON API -----------------------------------------------------------------

@bp.route("/api/status")
def api_status():
    nr = scheduler.next_run()
    api = agent.api_status()
    return jsonify(running=pipeline.is_running(), next_run=nr.isoformat() if nr else None,
                   counts=db.paper_counts(), last_run=(db.recent_runs(1) or [None])[0],
                   api={"ok": bool(api.get("ok")), "message": api.get("message")})


@bp.route("/api/papers")
def api_papers():
    papers = db.query_papers(sort=request.args.get("sort", "recent"), limit=min(request.args.get("limit", 50, type=int), 200))
    out = []
    for p in papers:
        r = p.get("review") or {}
        out.append({
            "id": p["id"], "title": p["title"], "title_ko": r.get("title_ko"), "url": p["url"],
            "pdf_url": p.get("pdf_url"), "published": p.get("published"),
            "scores": {"methodology": p["score_method"], "novelty": p["score_novelty"], "overall": p["score_overall"]},
            "one_liner": r.get("one_liner"), "tldr": r.get("tldr"), "tags": r.get("tags", []),
            "detail": url_for("web.paper_detail", paper_id=p["id"], _external=True),
        })
    return jsonify(out)
