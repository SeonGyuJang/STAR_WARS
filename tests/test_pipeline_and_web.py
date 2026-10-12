import json
import re
import shutil
from types import SimpleNamespace
from unittest import mock

from paperagent import agent, demo, pipeline


def _paper(**kw):
    p = {"uid": "arxiv:2610.01234", "source_kind": "arxiv", "source_name": "arXiv · cs.CL",
         "title": "Self-Verification Loops", "abstract": "LLM agents verify answers.", "authors": ["A. Kim"],
         "published": "2026-10-09", "url": "https://arxiv.org/abs/2610.01234",
         "pdf_url": "https://arxiv.org/pdf/2610.01234", "matched_keywords": ["AI agent"]}
    p.update(kw)
    return p


JUDGE_KEYS = ("methodology_score", "methodology_reason", "novelty_score", "novelty_reason", "read_confidence")
NOTES = {"research_question": "q", "method": "m", "claimed_contributions": ["c"], "experimental_setup": "s",
         "baselines": ["b"], "main_results": [{"claim": "c", "evidence": "+12.4%p on MATH", "location": "Table 1"}],
         "ablations_and_analysis": "a", "statistical_rigor": "3 seeds", "reproducibility": "code released",
         "stated_limitations": ["l"], "possible_concerns": ["p"], "closest_prior_work": "w",
         "figures": [{"number": 1, "shows": "accuracy bars"}], "headline_numbers": [{"value": "+12.4%p", "meaning": "MATH"}]}


def _stage_payloads():
    """What the reader, judge and writer each return, split from the sample review."""
    sample = json.loads(json.dumps(demo.SAMPLE_REVIEW))
    judge = {k: sample[k] for k in JUDGE_KEYS}
    judge.update(strengths=["s"], weaknesses=["w"], verdict="v")
    write = {k: v for k, v in sample.items()
             if k not in JUDGE_KEYS + ("strengths", "weaknesses", "verdict", "overall_score", "card") and not k.startswith("_")}
    return {id(agent.NOTES_SCHEMA): NOTES, id(agent.JUDGE_SCHEMA): judge, id(agent.WRITE_SCHEMA): write,
            id(agent.CARD_SCHEMA): sample["card"]}


def _fake_message(payload, stop_reason="end_turn", model="m", usage=(1000, 100)):
    return SimpleNamespace(stop_reason=stop_reason, stop_details=None, model=model,
                           usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]),
                           content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))])


def _fake_client(payload=None, stop_reason="end_turn", by_schema=None, usage=(1000, 100)):
    """Claude client double. Returns `payload`, or picks one by the request's schema."""
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        schema = kwargs["output_config"]["format"]["schema"]
        body = by_schema[id(schema)] if by_schema else payload
        cm = mock.MagicMock()
        cm.__enter__.return_value.get_final_message.return_value = _fake_message(
            body, stop_reason, model=kwargs["model"], usage=usage)
        return cm

    client = mock.Mock()
    client.beta.messages.stream.side_effect = stream
    client.messages.stream.side_effect = stream
    client.calls = calls
    return client


def test_review_uses_cheap_reader_and_strong_judge(fresh_db, sample_pdf):
    payloads = _stage_payloads()
    payloads[id(agent.JUDGE_SCHEMA)]["methodology_score"] = 11  # out of range -> clamped
    payloads[id(agent.CARD_SCHEMA)]["problem"]["figure_number"] = 7  # not extracted -> 0
    payloads[id(agent.CARD_SCHEMA)]["results"]["figure_number"] = 1  # same figure as idea -> 0
    client = _fake_client(by_schema=payloads, usage=(50_000, 2_000))
    with mock.patch.object(agent, "client", return_value=client):
        out, depth = agent.review(_paper(), ["AI agent"], sample_pdf, [{"number": 1, "page": 1, "caption": "Figure 1: x", "path": "x"}])
    read, judge, write, card = client.calls
    # 1. the cheap model gets the whole PDF; Haiku has no server-side fallback
    assert read["model"] == "claude-haiku-5-5" and "fallbacks" not in read
    doc = read["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf"
    # 2. the strong model only sees the notes, never the PDF
    assert judge["model"] == "claude-opus-5-5" and judge["fallbacks"] == "default"
    assert isinstance(judge["messages"][0]["content"], str) and "+12.4%p on MATH" in judge["messages"][0]["content"]
    # 3. the writer
    assert write["model"] == "claude-sonnet-5-5"
    assert depth == "full_pdf"
    assert out["methodology_score"] == 10.0 and out["overall_score"] == 9.0
    assert out["card"]["problem"]["figure_number"] == 0
    assert out["card"]["idea"]["figure_number"] == 1 and out["card"]["results"]["figure_number"] == 0
    # the reader and writer have room for the longer notes and review
    assert read["max_tokens"] >= 32000 and write["max_tokens"] >= 32000
    # the card copy is a separate, smaller request built from the written review
    assert card["model"] == "claude-sonnet-5-5" and "card" not in agent.WRITE_SCHEMA["properties"]
    assert demo.SAMPLE_REVIEW["one_liner"] in card["messages"][0]["content"]
    # cost: 50k in / 2k out on each model
    expected = (50_000 * 0.10 + 2_000 * 0.50 + 50_000 * 4 + 2_000 * 20 + 2 * (50_000 * 2 + 2_000 * 10)) / 1e6
    assert abs(out["_cost"]["total_usd"] - expected) < 1e-4
    assert [c["stage"] for c in out["_cost"]["calls"]] == ["read", "judge", "write", "card"]


def test_abstract_only_skips_reader(fresh_db):
    client = _fake_client(by_schema=_stage_payloads())
    with mock.patch.object(agent, "client", return_value=client):
        out, depth = agent.review(_paper(pdf_url=None), ["AI agent"])
    assert depth == "abstract" and out["read_confidence"] == "low"
    assert [c["model"] for c in client.calls] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-sonnet-5-5"]


def test_haiku_long_prompt_price():
    assert agent.price("claude-haiku-5-5", 100_000, 0) == 0.01
    assert agent.price("claude-haiku-5-5", 200_000, 0) == 0.1


def test_refusal_raises(fresh_db):
    client = _fake_client({}, stop_reason="refusal")
    with mock.patch.object(agent, "client", return_value=client):
        try:
            agent.review(_paper(), ["AI agent"])
        except agent.ReviewError as exc:
            assert "declined" in str(exc)
        else:
            raise AssertionError("expected ReviewError")


def test_screen_maps_indices(fresh_db):
    fresh_db.insert_paper(_paper())
    fresh_db.insert_paper(_paper(uid="arxiv:2610.00002", title="Other"))
    papers = fresh_db.papers_by_status("new")
    client = _fake_client({"results": [{"index": 0, "relevance": 8, "reason": "관련"}, {"index": 1, "relevance": 2, "reason": "무관"}, {"index": 9, "relevance": 9, "reason": "bogus"}]})
    meter = agent.CostMeter()
    with mock.patch.object(agent, "client", return_value=client):
        scores = agent.screen(papers, ["AI agent"], meter=meter)
    assert scores == {papers[0]["id"]: (8.0, "관련"), papers[1]["id"]: (2.0, "무관")}
    assert client.calls[0]["model"] == "claude-haiku-5-5" and meter.total_usd > 0


def _full_run(fresh_db, sample_pdf, tmp_path):
    """collect -> screen -> review -> cards, with network and Claude mocked."""
    downloaded = []

    def fake_download(paper, dest_dir):
        dst = dest_dir / "Self-Verification Loops.pdf"
        shutil.copy(sample_pdf, dst)
        downloaded.append(dst)
        return dst

    payloads = _stage_payloads()
    payloads[id(agent.SCREEN_SCHEMA)] = {"results": [{"index": 0, "relevance": 9, "reason": "핵심 주제"}]}
    client = _fake_client(by_schema=payloads)

    with mock.patch.object(pipeline.arxiv, "fetch_category", return_value=[_paper()]), \
         mock.patch.object(pipeline.openalex, "resolve_source", return_value=None), \
         mock.patch.object(pipeline.pdfs, "download_pdf", side_effect=fake_download), \
         mock.patch.object(pipeline.trending, "refresh_all"), \
         mock.patch.object(agent, "client", return_value=client):
        run_id = pipeline.run("test")

    run = fresh_db.recent_runs(1)[0]
    assert run["id"] == run_id and run["status"] == "ok", run["log"]
    assert run["stats"]["new"] == 1 and run["stats"]["reviewed"] == 1
    done = fresh_db.query_papers()
    assert len(done) == 1
    p = done[0]
    assert p["status"] == "done" and p["read_depth"] == "full_pdf"
    assert p["score_overall"] == 7.8 and len(p["card_paths"]) == 9
    assert [f["number"] for f in p["review"]["_figures"]] == [1, 2]
    # the PDF was only kept for the review, then deleted with its temp dir
    assert downloaded and not downloaded[0].exists() and not downloaded[0].parent.exists()
    assert p["pdf_url"] == "https://arxiv.org/pdf/2610.01234"
    # figures and cards live in storage, keyed by paper id
    assert p["review"]["_figures"][0]["key"] == f"figures/{p['id']}/fig1.png"
    assert p["card_paths"][0] == f"cards/{p['id']}/card_01.png"
    # screen + read + judge + write were all priced
    assert p["cost_usd"] > 0 and run["stats"]["cost_usd"] > p["cost_usd"]
    assert [c["stage"] for c in p["review"]["_cost"]["calls"]] == ["read", "judge", "write", "card"]
    return p


def test_full_run(fresh_db, sample_pdf, tmp_path):
    _full_run(fresh_db, sample_pdf, tmp_path)


def _csrf(client):
    html = client.get("/login").get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def test_web_pages_and_admin_actions(fresh_db, sample_pdf, tmp_path):
    p = _full_run(fresh_db, sample_pdf, tmp_path)
    from paperagent import create_app

    app = create_app(start_scheduler=False)
    c = app.test_client()
    for url in ["/", "/trending", "/queue", "/settings", f"/paper/{p['id']}", "/api/status", "/api/papers"]:
        assert c.get(url).status_code == 200, url
    assert demo.SAMPLE_REVIEW["title_ko"] in c.get("/").get_data(as_text=True)
    assert c.get(f"/files/card/{p['id']}/1").mimetype == "image/png"
    assert c.get(f"/files/cards/{p['id']}.zip").mimetype == "application/zip"
    assert c.get(f"/files/pdf/{p['id']}").status_code == 404  # PDFs are never served or kept
    assert "https://arxiv.org/pdf/2610.01234" in c.get(f"/paper/{p['id']}").get_data(as_text=True)
    assert c.get(f"/files/figure/{p['id']}/..%2Fsecret.png").status_code == 404

    assert c.post("/settings/keywords", data={"term": "world model"}).status_code == 400  # no CSRF
    token = _csrf(c)
    c.post("/settings/keywords", data={"term": "world model, mixture of experts", "_csrf": token})
    terms = [k["term"] for k in fresh_db.list_keywords()]
    assert "world model" in terms and "mixture of experts" in terms
    kid = next(k["id"] for k in fresh_db.list_keywords() if k["term"] == "world model")
    c.post(f"/settings/keywords/{kid}/delete", data={"_csrf": token})
    assert "world model" not in [k["term"] for k in fresh_db.list_keywords()]

    c.post("/settings/sources/arxiv", data={"category": "cs.RO", "_csrf": token})
    assert any(s["value"] == "cs.RO" for s in fresh_db.list_sources())
    c.post("/settings/sources/journal", data={"id": "S123", "name": "Test Journal", "_csrf": token})
    sid = next(s["id"] for s in fresh_db.list_sources() if s["value"] == "S123")
    c.post(f"/settings/sources/{sid}/delete", data={"_csrf": token})
    assert not any(s["value"] == "S123" for s in fresh_db.list_sources())

    c.post("/settings/pipeline", data={"interval_hours": "12", "lookback_days": "2", "max_per_source": "30",
                                       "max_full_reads": "3", "min_relevance": "7", "_csrf": token})
    assert fresh_db.pipeline_settings()["interval_hours"] == 12


def test_admin_password_gates_changes(fresh_db, monkeypatch):
    from paperagent import config, create_app

    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    c = create_app(start_scheduler=False).test_client()
    token = _csrf(c)
    r = c.post("/settings/keywords", data={"term": "blocked", "_csrf": token})
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert "blocked" not in [k["term"] for k in fresh_db.list_keywords()]
    c.post("/login", data={"password": "pw", "_csrf": token})
    c.post("/settings/keywords", data={"term": "allowed", "_csrf": token})
    assert "allowed" in [k["term"] for k in fresh_db.list_keywords()]


def test_is_due_follows_interval(fresh_db):
    assert pipeline.is_due()  # never ran
    run_id = fresh_db.start_run("test")
    fresh_db.finish_run(run_id, "ok", {})
    assert not pipeline.is_due()  # just ran, interval is 12h
    fresh_db.set_setting("interval_hours", 1)
    with fresh_db.engine().begin() as conn:
        conn.execute(fresh_db.runs.update().values(started_at=fresh_db.ago_iso(hours=2)))
    assert pipeline.is_due()


def test_run_skips_while_another_process_runs(fresh_db):
    other = fresh_db.start_run("schedule")  # e.g. a Heroku Scheduler dyno
    assert pipeline.is_running()
    assert pipeline.run("manual") is None
    # a run that died long ago no longer blocks
    with fresh_db.engine().begin() as conn:
        conn.execute(fresh_db.runs.update().where(fresh_db.runs.c.id == other).values(started_at=fresh_db.ago_iso(hours=5)))
    fresh_db.mark_stale_runs()
    assert fresh_db.recent_runs(1)[0]["status"] == "aborted"
    assert not pipeline.is_running()


def test_cli_run_if_due_skips_when_not_due(fresh_db, capsys):
    from paperagent.__main__ import main

    fresh_db.finish_run(fresh_db.start_run("test"), "ok", {})
    with mock.patch.object(pipeline, "run") as run:
        main(["run", "--if-due"])
    run.assert_not_called()
    assert "not due" in capsys.readouterr().out


def test_storage_roundtrip(fresh_db):
    from paperagent import storage

    storage.put("cards/99/card_01.png", b"\x89PNG data")
    assert storage.get("cards/99/card_01.png") == (b"\x89PNG data", "image/png")
    storage.delete_prefix("cards/99/")
    assert storage.get("cards/99/card_01.png") is None


def test_demo_seed_and_clear(fresh_db):
    from paperagent import create_app, storage

    pid = demo.seed()
    p = fresh_db.get_paper(pid)
    assert p["status"] == "done" and len(p["card_paths"]) == 9
    assert [f["number"] for f in p["review"]["_figures"]] == [1, 2]
    assert len(fresh_db.papers_by_status("screened")) == 2
    c = create_app(start_scheduler=False).test_client()
    html = c.get(f"/paper/{pid}").get_data(as_text=True)
    assert demo.SAMPLE_REVIEW["one_liner"] in html
    for part in ("핵심 아이디어", "실험 설정", "Table 1", "분석과 추가 실험", "기존 연구와 다른 점", "용어 정리", "강점"):
        assert part in html, part
    assert demo.clear() == 4
    assert fresh_db.get_paper(pid) is None and storage.get(f"cards/{pid}/card_01.png") is None


def test_init_db_adds_new_columns(fresh_db):
    """Deploying a version with a new column must not need a manual migration."""
    from sqlalchemy import inspect, text

    with fresh_db.engine().begin() as conn:
        conn.execute(text("ALTER TABLE papers DROP COLUMN cost_usd"))
    assert "cost_usd" not in {c["name"] for c in inspect(fresh_db.engine()).get_columns("papers")}
    fresh_db.init_db()
    assert "cost_usd" in {c["name"] for c in inspect(fresh_db.engine()).get_columns("papers")}


def test_schema_too_large_falls_back_to_unconstrained_json(fresh_db):
    """The API can refuse to compile a big schema; the call then asks for the JSON in the prompt instead."""
    import anthropic
    import httpx2 as httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    msg = ("The compiled grammar is too large, which would cause performance issues. "
           "Simplify your tool schemas or reduce the number of strict tools.")
    too_big = anthropic.BadRequestError(msg, response=httpx.Response(400, request=req), body=None)
    answer = {"methodology_score": 7, "methodology_reason": "r", "novelty_score": 6, "novelty_reason": "r",
              "strengths": ["s"], "weaknesses": ["w"], "verdict": "v", "read_confidence": "high"}
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise too_big
        cm = mock.MagicMock()
        text = "```json\n" + json.dumps(answer) + "\n```"
        cm.__enter__.return_value.get_final_message.return_value = SimpleNamespace(
            stop_reason="end_turn", stop_details=None, model=kwargs["model"],
            usage=SimpleNamespace(input_tokens=10, output_tokens=10), content=[SimpleNamespace(type="text", text=text)])
        return cm

    client = mock.Mock()
    client.beta.messages.stream.side_effect = stream
    client.messages.stream.side_effect = stream
    meter = agent.CostMeter()
    with mock.patch.object(agent, "client", return_value=client):
        out = agent.judge(_paper(), ["AI agent"], NOTES, "full_pdf", meter)
    assert out["methodology_score"] == 7.0 and out["verdict"] == "v"
    assert "format" in calls[0]["output_config"] and "format" not in calls[1]["output_config"]
    assert '"methodology_score"' in calls[1]["system"]  # the schema moved into the instructions
    assert [c["stage"] for c in meter.calls] == ["judge"]  # only the call that ran is billed


def test_failed_review_still_counts_what_it_spent(fresh_db, sample_pdf, tmp_path):
    fresh_db.insert_paper(_paper())
    paper = fresh_db.get_paper_by_uid("arxiv:2610.01234")
    err = agent.ReviewError("write: response truncated at max_tokens")
    err.cost_usd = 0.042

    def review(*a, **k):
        raise err

    pdf = tmp_path / "p.pdf"
    shutil.copy(sample_pdf, pdf)
    with mock.patch.object(pipeline.pdfs, "download_pdf", return_value=pdf), mock.patch.object(agent, "review", side_effect=review):
        ok, cost = pipeline.process_paper(paper, ["AI agent"])
    p = fresh_db.get_paper(paper["id"])
    assert not ok and cost == 0.042 and p["status"] == "error" and p["cost_usd"] == 0.042


def test_post_retries_rate_limits():
    from paperagent import collectors

    limited = mock.Mock(status_code=429)
    ok = mock.Mock(status_code=200, raise_for_status=lambda: None)
    with mock.patch.object(collectors._session, "request", side_effect=[limited, ok]) as req, \
         mock.patch.object(collectors.time, "sleep") as sleep:
        assert collectors.http_post("https://api.semanticscholar.org/x", json={}) is ok
    assert req.call_count == 2 and sleep.call_count == 1


def test_recent_sort_follows_review_time(fresh_db, monkeypatch):
    """'최근 분석순' lists papers by when their review finished, not when they were collected."""
    from paperagent import create_app, db

    stamps = iter(["2026-10-10T01:00:00+00:00", "2026-10-10T02:00:00+00:00"])  # collected: old first, new second
    monkeypatch.setattr(db, "now_iso", lambda: next(stamps, "2026-10-12T09:00:00+00:00"))
    for uid, title in (("arxiv:old", "Collected first, reviewed last"), ("arxiv:new", "Collected last, reviewed first")):
        db.insert_paper({"uid": uid, "source_kind": "arxiv", "title": title})
    db.update_paper(db.get_paper_by_uid("arxiv:new")["id"], status="done", score_overall=7, reviewed_at="2026-10-11T08:00:00+00:00")
    db.update_paper(db.get_paper_by_uid("arxiv:old")["id"], status="done", score_overall=7, reviewed_at="2026-10-12T08:00:00+00:00")
    assert [p["uid"] for p in db.query_papers(sort="recent")] == ["arxiv:old", "arxiv:new"]
    html = create_app(start_scheduler=False).test_client().get("/?sort=recent").get_data(as_text=True)
    assert html.index("Collected first, reviewed last") < html.index("Collected last, reviewed first")
    assert "10.12 17:00 분석" in html  # shown in Korea time


def test_old_reviews_get_their_real_review_time(fresh_db):
    """Reviews made before reviewed_at existed get the time from the run logs
    (UTC-stamped older logs and KST-stamped newer ones), else from their cards."""
    from paperagent import db, storage

    ids = {}
    for uid in ("arxiv:a", "arxiv:b", "arxiv:c", "arxiv:d"):
        db.insert_paper({"uid": uid, "source_kind": "arxiv", "title": uid})
        ids[uid] = db.get_paper_by_uid(uid)["id"]
        db.update_paper(ids[uid], status="done", card_paths=[f"cards/{ids[uid]}/card_01.png"] if uid == "arxiv:c" else [])
    a, b, d = ids["arxiv:a"], ids["arxiv:b"], ids["arxiv:d"]
    with db.engine().begin() as conn:
        # older run, log stamped in UTC: started 2026-10-10 23:50 UTC, b reviewed after midnight
        conn.execute(db.runs.insert().values(trigger="t", status="ok", started_at="2026-10-10T23:50:00+00:00", log=(
            f"[23:50:01] arXiv cs.CL: 3 matched, 3 new\n[23:58:00] reading #{b}: B\n[00:02:10]   reviewed (full_pdf): method 7\n")))
        # newer run, log stamped in Korea time: started 2026-10-12 01:00 UTC = 10:00 KST
        conn.execute(db.runs.insert().values(trigger="t", status="ok", started_at="2026-10-12T01:00:00+00:00", log=(
            f"[10:00:02] arXiv cs.CL: 1 matched\n[10:01:00] reading #{a}: A\n[10:03:30]   reviewed (full_pdf): method 8\n"
            f"[10:04:00] reading #{d}: D\n[10:05:00]   review failed: boom\n")))
        conn.execute(db.papers.update().values(reviewed_at=None, updated_at="2026-10-12T05:00:00+00:00"))
    storage.put(f"cards/{ids['arxiv:c']}/card_01.png", b"png")
    db.set_setting("reviewed_at_v2", False)
    db.init_db()
    assert db.get_paper(b)["reviewed_at"] == "2026-10-11T00:02:10+00:00"
    assert db.get_paper(a)["reviewed_at"] == "2026-10-12T01:03:30+00:00"
    assert db.get_paper(ids["arxiv:c"])["reviewed_at"] == storage.stored_at(f"cards/{ids['arxiv:c']}/card_01.png")
    assert db.get_paper(d)["reviewed_at"] == "2026-10-12T05:00:00+00:00"  # failed review: no evidence, last update
    # citation refreshes no longer look like edits
    before = db.get_paper(a)["updated_at"]
    db.set_citation_count(a, 12)
    assert db.get_paper(a)["updated_at"] == before and db.get_paper(a)["citation_count"] == 12


def test_instagram_posted_mark_and_list(fresh_db):
    import re as _re

    from paperagent import create_app, db

    for uid, title in (("arxiv:p1", "Posted paper"), ("arxiv:p2", "Not posted paper")):
        db.insert_paper({"uid": uid, "source_kind": "arxiv", "title": title})
        db.update_paper(db.get_paper_by_uid(uid)["id"], status="done", score_overall=7, reviewed_at=db.now_iso())
    pid = db.get_paper_by_uid("arxiv:p1")["id"]
    app = create_app(start_scheduler=False)
    admin = app.test_client()
    admin.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    token = _re.search(r'name="csrf-token" content="([^"]+)"', admin.get("/").get_data(as_text=True)).group(1)
    assert "인스타 업로드 완료" in admin.get("/").get_data(as_text=True)  # the button, for the admin

    # the page's script posts with Accept: application/json and updates in place
    r = admin.post(f"/paper/{pid}/instagram", data={"_csrf": token, "posted": "1"}, headers={"Accept": "application/json"})
    assert r.get_json()["posted"] is True and r.get_json()["counts"] == {"posted": 1, "pending": 1}
    assert db.get_paper(pid)["instagram_posted_at"]

    posted = admin.get("/?ig=posted").get_data(as_text=True)
    assert "Posted paper" in posted and "Not posted paper" not in posted
    pending = admin.get("/?ig=pending").get_data(as_text=True)
    assert "Not posted paper" in pending and "Posted paper" not in pending

    # visitors see the posted list and a badge, but not the button or the 'not yet' list
    visitor = app.test_client()
    visitor.environ_base["REMOTE_ADDR"] = "10.0.0.9"
    html = visitor.get("/?ig=posted").get_data(as_text=True)
    assert "Posted paper" in html and "인스타 게시" in html and "아직 안 올림" not in html and "data-ig>" not in html
    assert "Not posted paper" in visitor.get("/?ig=pending").get_data(as_text=True)  # ignored -> everything
    assert visitor.post(f"/paper/{pid}/instagram", data={"_csrf": token}).status_code in (302, 400)  # refused
    assert db.get_paper(pid)["instagram_posted_at"]

    # a plain form post (no script) toggles it back off
    admin.post(f"/paper/{pid}/instagram", data={"_csrf": token})
    assert db.get_paper(pid)["instagram_posted_at"] is None
