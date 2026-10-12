"""API outages, trends without the API, hand-picked papers, admin-only settings, KST."""
import re
from unittest import mock

import anthropic
import httpx2 as httpx  # the HTTP client the anthropic SDK uses

from paperagent import agent, config, create_app, db, pipeline
from paperagent.collectors import openalex, trending


def _paper(uid, **kw):
    p = {"uid": uid, "source_kind": "arxiv", "source_name": "arXiv · cs.CL", "title": f"Paper {uid}",
         "abstract": "LLM agents.", "authors": ["A"], "published": "2026-10-09",
         "url": f"https://arxiv.org/abs/{uid}", "pdf_url": None, "matched_keywords": ["AI agent"]}
    p.update(kw)
    return p


def _auth_error():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.AuthenticationError("invalid x-api-key", response=httpx.Response(401, request=req), body=None)


def _client(remote="127.0.0.1"):
    c = create_app(start_scheduler=False).test_client()
    c.environ_base["REMOTE_ADDR"] = remote
    return c


def _csrf(c):
    return re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").get_data(as_text=True)).group(1)


# --- trends need no Claude API -------------------------------------------------

HF = [{"paper": {"id": "2610.00001", "title": "Hot Paper", "upvotes": 42, "summary": "s", "authors": [{"name": "A"}],
                 "publishedAt": "2026-10-10"}, "numComments": 3}]
WORK = {"id": "https://openalex.org/W1", "display_name": "Cited Paper", "doi": "https://doi.org/10.1/x",
        "publication_date": "2025-06-01", "cited_by_count": 120, "authorships": [{"author": {"display_name": "B"}}],
        "primary_location": {"landing_page_url": "https://example.org/p", "source": {"display_name": "JMLR"}},
        "counts_by_year": [{"year": 2026, "cited_by_count": 90}, {"year": 2025, "cited_by_count": 30}]}


def _fake_get(url, params=None, **kw):
    if "huggingface" in url:
        return mock.Mock(json=lambda: HF)
    if "openalex" in url:
        return mock.Mock(json=lambda: {"results": [WORK]})
    raise AssertionError(url)


def test_trending_panels_fill_without_claude(fresh_db):
    with mock.patch.object(trending, "http_get", side_effect=_fake_get), \
         mock.patch.object(openalex, "http_get", side_effect=_fake_get), \
         mock.patch.object(agent, "client", side_effect=AssertionError("Claude must not be called")):
        pipeline.refresh_trending_only()
    hf, _ = fresh_db.get_trending("hf_daily")
    surge, _ = fresh_db.get_trending("surge")
    active, _ = fresh_db.get_trending("active")
    assert hf[0]["title"] == "Hot Paper" and hf[0]["upvotes"] == 42
    assert surge[0]["title"] == "Cited Paper" and surge[0]["this_year"] == 90
    assert active[0]["citation_count"] == 120
    html = _client().get("/trending").get_data(as_text=True)
    assert "Hot Paper" in html and "Cited Paper" in html


def test_tracked_citations_need_no_claude(fresh_db):
    fresh_db.insert_paper(_paper("arxiv:2610.00009"))
    pid = fresh_db.get_paper_by_uid("arxiv:2610.00009")["id"]
    fresh_db.update_paper(pid, status="done")
    with mock.patch.object(trending, "http_post", return_value=mock.Mock(json=lambda: [{"citationCount": 7}])):
        trending.refresh_tracked_citations()
    assert fresh_db.get_paper(pid)["citation_count"] == 7


# --- a broken or missing API stops only the AI steps --------------------------

def _run_with(client_side_effect=None, key="test-key"):
    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": key}), \
         mock.patch.object(pipeline.arxiv, "fetch_category", return_value=[_paper("arxiv:2610.00002")]), \
         mock.patch.object(pipeline.openalex, "resolve_source", return_value=None), \
         mock.patch.object(pipeline.trending, "refresh_all") as refresh, \
         mock.patch.object(agent, "client", side_effect=client_side_effect):
        run_id = pipeline.run("test")
    return run_id, refresh


def test_bad_api_key_still_collects_and_refreshes_trends(fresh_db):
    broken = mock.Mock()
    broken.messages.stream.side_effect = _auth_error()
    run_id, refresh = _run_with(client_side_effect=lambda: broken)
    run = fresh_db.recent_runs(1)[0]
    assert run["id"] == run_id and run["status"] == "partial"
    assert run["stats"]["new"] == 1 and "API 키가 올바르지 않습니다" in run["stats"]["api_error"]
    refresh.assert_called_once()  # trends refreshed even though Claude failed
    assert fresh_db.papers_by_status("new")[0]["uid"] == "arxiv:2610.00002"  # left for the next run
    assert agent.api_status()["ok"] is False
    # admin sees the reason and the check button; visitors see a gentle note
    admin_html = _client().get("/").get_data(as_text=True)
    assert "Claude API를 쓸 수 없어 AI 리뷰가 멈춰 있습니다" in admin_html and "API 다시 확인" in admin_html
    public_html = _client("10.1.2.3").get("/").get_data(as_text=True)
    assert "새 논문 리뷰가 잠시 멈춰 있습니다" in public_html and "API 다시 확인" not in public_html
    # partial runs don't count as done, so the hourly job retries once the key is fixed
    assert pipeline.is_due()


def test_low_credit_balance_is_a_billing_problem(fresh_db):
    # the exact 400 the API returns when the account runs out of credit
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    msg = ("Your credit balance is too low to access the Anthropic API. "
           "Please go to Plans & Billing to upgrade or purchase credits.")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": msg}}
    err = anthropic.BadRequestError(msg, response=httpx.Response(400, request=req), body=body)
    broken = mock.Mock()
    broken.messages.stream.side_effect = err
    _, refresh = _run_with(client_side_effect=lambda: broken)
    run = fresh_db.recent_runs(1)[0]
    assert run["status"] == "partial" and "크레딧" in run["stats"]["api_error"]
    refresh.assert_called_once()
    assert agent.api_status()["kind"] == "billing"


def test_missing_api_key_skips_ai_without_calling_claude(fresh_db):
    run_id, refresh = _run_with(client_side_effect=AssertionError("no client without a key"), key="")
    run = fresh_db.recent_runs(1)[0]
    assert run["status"] == "partial" and "설정되지 않았습니다" in run["stats"]["api_error"]
    refresh.assert_called_once()
    assert agent.api_status()["kind"] == "missing_key"


def test_reading_buttons_explain_api_problem(fresh_db):
    fresh_db.insert_paper(_paper("arxiv:2610.00003"))
    pid = fresh_db.get_paper_by_uid("arxiv:2610.00003")["id"]
    c = _client()
    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}), \
         mock.patch.object(pipeline, "run_in_background") as bg:
        html = c.post(f"/paper/{pid}/reprocess", data={"_csrf": _csrf(c)}, follow_redirects=True).get_data(as_text=True)
    bg.assert_not_called()
    assert "API 키와 결제 상태를 확인" in html


def test_api_check_button_clears_notice(fresh_db):
    agent.record_api_problem("auth", "API 키가 올바르지 않습니다 (401).")
    ok_client = mock.Mock()
    with mock.patch.object(agent, "client", return_value=ok_client):
        c = _client()
        html = c.post("/settings/api-check", data={"_csrf": _csrf(c)}, follow_redirects=True).get_data(as_text=True)
    assert ok_client.models.retrieve.called and agent.api_status()["ok"] is True
    assert "Claude API가 정상입니다" in html


# --- relevance below the threshold, picked by hand ------------------------------

def test_skipped_papers_can_be_picked_for_reading(fresh_db):
    for uid, status, rel in [("arxiv:2610.00004", "skipped", 3.0), ("arxiv:2610.00005", "screened", 9.0)]:
        fresh_db.insert_paper(_paper(uid))
        fresh_db.update_paper(fresh_db.get_paper_by_uid(uid)["id"], status=status, relevance=rel)
    low = fresh_db.get_paper_by_uid("arxiv:2610.00004")["id"]
    c = _client()
    html = c.get("/queue").get_data(as_text=True)
    assert "관련도 미달" in html and 'name="paper_id"' in html
    c.post("/queue/read", data={"_csrf": _csrf(c), "paper_id": [str(low)], "when": "next"})
    p = fresh_db.get_paper(low)
    assert p["status"] == "screened" and p["priority"] == 1
    # the next run reads the hand-picked paper first, even with no automatic slots left
    fresh_db.set_setting("max_full_reads", 0)
    read = []
    with mock.patch.object(pipeline, "collect", return_value=0), mock.patch.object(pipeline, "resolve_journals"), \
         mock.patch.object(pipeline, "screen_new", return_value=0), mock.patch.object(pipeline.trending, "refresh_all"), \
         mock.patch.object(pipeline, "process_paper", side_effect=lambda p, *a: read.append(p["id"]) or (True, 0.1)):
        pipeline.run("test")
    assert read == [low]


def test_only_papers_at_or_above_threshold_are_read_automatically(fresh_db):
    # leftovers from older runs: 'screened' under an earlier, lower threshold
    rels = {"arxiv:2610.00011": 9.5, "arxiv:2610.00012": 8.0, "arxiv:2610.00013": 6.5,
            "arxiv:2610.00014": 5.0, "arxiv:2610.00015": 7.0}
    for uid, rel in rels.items():
        fresh_db.insert_paper(_paper(uid))
        fresh_db.update_paper(fresh_db.get_paper_by_uid(uid)["id"], status="screened", relevance=rel)
    fresh_db.set_setting("min_relevance", 7.0)
    fresh_db.set_setting("max_full_reads", 5)
    read = []
    with mock.patch.object(pipeline, "collect", return_value=0), mock.patch.object(pipeline, "resolve_journals"), \
         mock.patch.object(pipeline, "screen_new", return_value=0), mock.patch.object(pipeline.trending, "refresh_all"), \
         mock.patch.object(pipeline, "process_paper", side_effect=lambda p, *a: read.append(p["relevance"]) or (True, 0.1)):
        pipeline.run("test")
    assert read == [9.5, 8.0, 7.0]  # 5 slots, but only three papers reach 7
    for uid, rel in rels.items():
        if rel < 7:
            assert fresh_db.get_paper_by_uid(uid)["status"] == "skipped"


def test_max_full_reads_caps_automatic_reviews(fresh_db):
    for i, rel in enumerate([9.0, 8.5, 8.0, 7.5]):
        uid = f"arxiv:2610.0002{i}"
        fresh_db.insert_paper(_paper(uid))
        fresh_db.update_paper(fresh_db.get_paper_by_uid(uid)["id"], status="screened", relevance=rel)
    fresh_db.set_setting("max_full_reads", 2)
    read = []
    with mock.patch.object(pipeline, "collect", return_value=0), mock.patch.object(pipeline, "resolve_journals"), \
         mock.patch.object(pipeline, "screen_new", return_value=0), mock.patch.object(pipeline.trending, "refresh_all"), \
         mock.patch.object(pipeline, "process_paper", side_effect=lambda p, *a: read.append(p["relevance"]) or (True, 0.1)):
        pipeline.run("test")
    assert read == [9.0, 8.5]


def test_changing_threshold_moves_papers_between_queues(fresh_db):
    for uid, status, rel in [("arxiv:2610.00031", "screened", 6.0), ("arxiv:2610.00032", "skipped", 6.0),
                             ("arxiv:2610.00033", "screened", 4.0)]:
        fresh_db.insert_paper(_paper(uid))
        fresh_db.update_paper(fresh_db.get_paper_by_uid(uid)["id"], status=status, relevance=rel)
    picked = fresh_db.get_paper_by_uid("arxiv:2610.00033")["id"]
    fresh_db.queue_papers([picked])  # hand-picked papers stay put
    c = _client()
    c.post("/settings/pipeline", data={"_csrf": _csrf(c), "interval_hours": 12, "lookback_days": 3,
                                       "max_per_source": 40, "max_full_reads": 3, "min_relevance": 5})
    assert fresh_db.get_paper_by_uid("arxiv:2610.00032")["status"] == "screened"
    c.post("/settings/pipeline", data={"_csrf": _csrf(c), "interval_hours": 12, "lookback_days": 3,
                                       "max_per_source": 40, "max_full_reads": 3, "min_relevance": 8})
    assert fresh_db.get_paper_by_uid("arxiv:2610.00031")["status"] == "skipped"
    assert fresh_db.get_paper_by_uid("arxiv:2610.00032")["status"] == "skipped"
    assert fresh_db.get_paper(picked)["status"] == "screened"


def test_long_lists_are_collapsible(fresh_db):
    fresh_db.save_trending("hf_daily", [{"title": f"Hot {i}", "url": "https://x", "upvotes": i, "comments": 0,
                                         "authors": ["A"], "arxiv_id": "2610.00001"} for i in range(20)])
    html = _client().get("/trending").get_data(as_text=True)
    assert 'id="hf" data-more="5"' in html
    for i in range(3):
        uid = f"arxiv:2610.0004{i}"
        fresh_db.insert_paper(_paper(uid))
        fresh_db.update_paper(fresh_db.get_paper_by_uid(uid)["id"], status="screened", relevance=9.0)
    assert 'id="screened" data-more="5"' in _client().get("/queue").get_data(as_text=True)


def test_pick_now_starts_reading(fresh_db):
    fresh_db.insert_paper(_paper("arxiv:2610.00006"))
    pid = fresh_db.get_paper_by_uid("arxiv:2610.00006")["id"]
    fresh_db.update_paper(pid, status="skipped", relevance=2.0)
    c = _client()
    with mock.patch.object(pipeline, "run_in_background", return_value=True) as bg:
        c.post("/queue/read", data={"_csrf": _csrf(c), "paper_id": [str(pid)], "when": "now"})
    bg.assert_called_once_with("manual", paper_ids=[pid])


# --- settings are admin-only ------------------------------------------------------

def test_settings_hidden_without_login(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    c = _client()
    r = c.get("/settings")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert 'href="/settings"' not in c.get("/").get_data(as_text=True)
    c.post("/login", data={"password": "pw", "_csrf": _csrf(c)})
    assert c.get("/settings").status_code == 200
    assert 'href="/settings"' in c.get("/").get_data(as_text=True)


def test_public_server_without_password_has_no_admin(fresh_db):
    c = _client("10.1.2.3")  # e.g. a request through the Heroku router
    assert c.get("/settings").status_code == 302
    assert "ADMIN_PASSWORD" in c.get("/login").get_data(as_text=True)
    assert _client("127.0.0.1").get("/settings").status_code == 200  # local testing stays easy


# --- times in Korea time ----------------------------------------------------------------

def test_times_are_shown_in_kst(fresh_db):
    from paperagent.web import to_local

    assert to_local("2026-10-11T06:30:00+00:00") == "2026-10-11 15:30"
    run_id = fresh_db.start_run("test")
    with mock.patch.object(db, "datetime") as dt:
        from datetime import datetime, timezone
        dt.now.side_effect = lambda tz=None: datetime(2026, 10, 11, 6, 30, 5, tzinfo=timezone.utc).astimezone(tz)
        fresh_db.append_run_log(run_id, "hello")
    assert fresh_db.recent_runs(1)[0]["log"].startswith("[15:30:05] hello")


def test_keyword_sidebar_is_a_vertical_list(fresh_db):
    html = _client().get("/").get_data(as_text=True)
    assert 'class="kw-vlist collapsed"' in html and "전체 보기" in html
