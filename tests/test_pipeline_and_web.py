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
             if k not in JUDGE_KEYS + ("strengths", "weaknesses", "verdict", "overall_score") and not k.startswith("_")}
    return {id(agent.NOTES_SCHEMA): NOTES, id(agent.JUDGE_SCHEMA): judge, id(agent.WRITE_SCHEMA): write}


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
    payloads[id(agent.WRITE_SCHEMA)]["card"]["slides"][0]["figure_number"] = 7  # not extracted -> 0
    client = _fake_client(by_schema=payloads, usage=(50_000, 2_000))
    with mock.patch.object(agent, "client", return_value=client):
        out, depth = agent.review(_paper(), ["AI agent"], sample_pdf, [{"number": 1, "page": 1, "caption": "Figure 1: x", "path": "x"}])
    read, judge, write = client.calls
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
    assert out["card"]["slides"][0]["figure_number"] == 0
    # cost: 50k in / 2k out on each model
    expected = (50_000 * 0.10 + 2_000 * 0.50 + 50_000 * 4 + 2_000 * 20 + 50_000 * 2 + 2_000 * 10) / 1e6
    assert abs(out["_cost"]["total_usd"] - expected) < 1e-4
    assert [c["stage"] for c in out["_cost"]["calls"]] == ["read", "judge", "write"]


def test_abstract_only_skips_reader(fresh_db):
    client = _fake_client(by_schema=_stage_payloads())
    with mock.patch.object(agent, "client", return_value=client):
        out, depth = agent.review(_paper(pdf_url=None), ["AI agent"])
    assert depth == "abstract" and out["read_confidence"] == "low"
    assert [c["model"] for c in client.calls] == ["claude-opus-5-5", "claude-sonnet-5-5"]


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
    assert p["score_overall"] == 7.8 and len(p["card_paths"]) == 6
    assert [f["number"] for f in p["review"]["_figures"]] == [1, 2]
    # the PDF was only kept for the review, then deleted with its temp dir
    assert downloaded and not downloaded[0].exists() and not downloaded[0].parent.exists()
    assert p["pdf_url"] == "https://arxiv.org/pdf/2610.01234"
    # figures and cards live in storage, keyed by paper id
    assert p["review"]["_figures"][0]["key"] == f"figures/{p['id']}/fig1.png"
    assert p["card_paths"][0] == f"cards/{p['id']}/card_01.png"
    # screen + read + judge + write were all priced
    assert p["cost_usd"] > 0 and run["stats"]["cost_usd"] > p["cost_usd"]
    assert [c["stage"] for c in p["review"]["_cost"]["calls"]] == ["read", "judge", "write"]
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
    assert p["status"] == "done" and len(p["card_paths"]) == 6
    assert [f["number"] for f in p["review"]["_figures"]] == [1, 2]
    assert len(fresh_db.papers_by_status("screened")) == 2
    c = create_app(start_scheduler=False).test_client()
    assert demo.SAMPLE_REVIEW["one_liner"] in c.get(f"/paper/{pid}").get_data(as_text=True)
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
