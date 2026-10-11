import json
import re
import shutil
from types import SimpleNamespace
from unittest import mock

from conftest import FIXTURES
from paperagent import agent, pipeline


def _paper(**kw):
    p = {"uid": "arxiv:2610.01234", "source_kind": "arxiv", "source_name": "arXiv · cs.CL",
         "title": "Self-Verification Loops", "abstract": "LLM agents verify answers.", "authors": ["A. Kim"],
         "published": "2026-10-09", "url": "https://arxiv.org/abs/2610.01234",
         "pdf_url": "https://arxiv.org/pdf/2610.01234", "matched_keywords": ["AI agent"]}
    p.update(kw)
    return p


def _fake_message(payload, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, stop_details=None,
                           content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))])


def _fake_client(payload, stop_reason="end_turn"):
    stream_cm = mock.MagicMock()
    stream_cm.__enter__.return_value.get_final_message.return_value = _fake_message(payload, stop_reason)
    client = mock.Mock()
    client.beta.messages.stream.return_value = stream_cm
    return client


def test_review_request_shape_and_postprocessing(sample_pdf):
    review = json.loads((FIXTURES / "sample_review.json").read_text())
    review["methodology_score"] = 11  # out of range -> clamped
    review["card"]["slides"][0]["figure_number"] = 7  # not extracted -> 0
    client = _fake_client(review)
    with mock.patch.object(agent, "client", return_value=client):
        out, depth = agent.review(_paper(), ["AI agent"], sample_pdf, [{"number": 1, "page": 1, "caption": "Figure 1: x", "path": "x"}])
    kwargs = client.beta.messages.stream.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["fallbacks"] == "default" and kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    doc = kwargs["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf"
    assert depth == "full_pdf"
    assert out["methodology_score"] == 10.0
    assert out["overall_score"] == 9.0
    assert out["card"]["slides"][0]["figure_number"] == 0


def test_refusal_raises():
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
    with mock.patch.object(agent, "client", return_value=client):
        scores = agent.screen(papers, ["AI agent"])
    assert scores == {papers[0]["id"]: (8.0, "관련"), papers[1]["id"]: (2.0, "무관")}


def _full_run(fresh_db, sample_pdf, tmp_path):
    """collect -> screen -> review -> cards, with network and Claude mocked."""
    review = json.loads((FIXTURES / "sample_review.json").read_text())

    downloaded = []

    def fake_download(paper, dest_dir):
        dst = dest_dir / "Self-Verification Loops.pdf"
        shutil.copy(sample_pdf, dst)
        downloaded.append(dst)
        return dst

    screen_client = {"results": [{"index": 0, "relevance": 9, "reason": "핵심 주제"}]}
    calls = []

    def fake_call(model, system, content, schema, effort, max_tokens):
        calls.append(schema)
        return json.loads(json.dumps(screen_client if schema is agent.SCREEN_SCHEMA else review))

    with mock.patch.object(pipeline.arxiv, "fetch_category", return_value=[_paper()]), \
         mock.patch.object(pipeline.openalex, "resolve_source", return_value=None), \
         mock.patch.object(pipeline.pdfs, "download_pdf", side_effect=fake_download), \
         mock.patch.object(pipeline.trending, "refresh_all"), \
         mock.patch.object(agent, "_call", side_effect=fake_call):
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
    return p


def test_full_run(fresh_db, sample_pdf, tmp_path):
    _full_run(fresh_db, sample_pdf, tmp_path)


def _csrf(client):
    html = client.get("/settings").get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def test_web_pages_and_admin_actions(fresh_db, sample_pdf, tmp_path):
    p = _full_run(fresh_db, sample_pdf, tmp_path)
    from paperagent import create_app

    app = create_app(start_scheduler=False)
    c = app.test_client()
    for url in ["/", "/trending", "/queue", "/settings", f"/paper/{p['id']}", "/api/status", "/api/papers"]:
        assert c.get(url).status_code == 200, url
    assert "작은 모델도" in c.get("/").get_data(as_text=True)
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
