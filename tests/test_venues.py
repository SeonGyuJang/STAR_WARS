"""Journals and conferences: finding them by name and collecting their papers."""
from datetime import datetime, timedelta, timezone
from unittest import mock

from paperagent import db, pipeline
from paperagent.collectors import openalex, venues

KEYWORDS = ["large language model", "AI agent"]


def _entry(arxiv_id, title, comment="", journal_ref="", days_ago=2):
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    extra = (f"<arxiv:comment>{comment}</arxiv:comment>" if comment else "") + \
            (f"<arxiv:journal_ref>{journal_ref}</arxiv:journal_ref>" if journal_ref else "")
    return f"""<entry><id>http://arxiv.org/abs/{arxiv_id}v2</id><updated>{stamp}</updated><published>{stamp}</published>
      <title>{title}</title><summary>An AI agent built on a large language model.</summary>
      <author><name>A. Kim</name></author>{extra}
      <arxiv:primary_category term="cs.LG"/><category term="cs.LG"/></entry>"""


def _feed(*entries):
    return ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'
            + "".join(entries) + "</feed>")


def test_short_conference_names_are_recognised():
    for typed in ("NeurIPS", "neurips 2025", "NIPS", "Advances in Neural Information Processing Systems"):
        assert venues.find_known(typed)["short"] == "NeurIPS", typed
    assert venues.find_known("T-PAMI")["short"] == "TPAMI"
    assert venues.find_known("Some Unknown Workshop") is None


def test_search_offers_known_venue_even_when_openalex_is_down():
    with mock.patch.object(openalex, "search_sources", side_effect=ConnectionError("blocked")):
        found, error = venues.search("NeurIPS")
    assert found[0]["name"] == "NeurIPS" and found[0]["type"] == "conference" and error


def test_search_lists_openalex_candidates_under_the_full_name():
    calls = []

    def search_sources(q, limit=8):
        calls.append(q)
        return [{"id": "S1", "name": "Neural Information Processing Systems", "type": "conference", "works_count": 30000}]

    with mock.patch.object(openalex, "search_sources", side_effect=search_sources):
        found, error = venues.search("NeurIPS")
    assert calls[0] == "Neural Information Processing Systems"  # the short name alone finds nothing in OpenAlex
    assert [c["name"] for c in found] == ["NeurIPS", "Neural Information Processing Systems"] and error is None


def test_arxiv_venue_papers_need_a_real_mention():
    feed = _feed(
        _entry("2610.10001", "Agents A", comment="Accepted at NeurIPS 2025 (spotlight)"),
        _entry("2610.10002", "Agents B", journal_ref="Advances in Neural Information Processing Systems 38 (2025)"),
        _entry("2610.10003", "Agents C", comment="code: https://neurips.cc/x"),  # URL, not an acceptance
        _entry("2610.10004", "Agents D", comment="Accepted at NeurIPS 2023", days_ago=900),  # too old
    )
    with mock.patch.object(venues.arxiv, "http_get", return_value=mock.Mock(text=feed)) as get:
        found = venues.fetch_arxiv("NeurIPS", venues.KNOWN["neurips"]["arxiv_terms"], KEYWORDS, 400, 40)
    assert [p["uid"] for p in found] == ["arxiv:2610.10001", "arxiv:2610.10002"]
    params = get.call_args.kwargs["params"]
    assert 'co:"NeurIPS"' in params["search_query"] and params["sortBy"] == "lastUpdatedDate"


def test_openalex_venue_counts_any_location():
    resp = mock.Mock(json=lambda: {"results": []})
    with mock.patch.object(openalex, "http_get", return_value=resp) as get:
        venues.fetch_openalex("S123", KEYWORDS, 400, 40)
    assert get.call_args.kwargs["params"]["filter"].startswith("locations.source.id:S123,")


def test_neurips_added_by_name_is_collected(fresh_db):
    sid = db.add_source("journal", "name:NeurIPS", "NeurIPS")
    # a paper we already had from an arXiv category gets tagged with the venue
    db.insert_paper({"uid": "arxiv:2610.10002", "source_kind": "arxiv", "source_name": "arXiv · cs.LG", "title": "B"})
    feed = _feed(_entry("2610.10001", "Agents A", comment="NeurIPS 2025"), _entry("2610.10002", "Agents B", comment="NeurIPS 2025"))
    say = []
    with mock.patch.object(venues.openalex, "search_sources", return_value=[]), \
         mock.patch.object(venues.arxiv, "http_get", return_value=mock.Mock(text=feed)), \
         mock.patch.object(pipeline.arxiv, "fetch_categories", return_value=[]):
        pipeline.resolve_journals(say.append)
        new = pipeline.collect(KEYWORDS, db.pipeline_settings(), say.append)
    assert new == 1
    assert any("arXiv comments only" in m for m in say)
    assert any(m.startswith("NeurIPS: 2 matched (arXiv 2), 1 new") for m in say)
    a = db.get_paper_by_uid("arxiv:2610.10001")
    assert a["source_id"] == sid and a["venue"] == "NeurIPS" and a["source_name"] == "NeurIPS"
    assert db.get_paper_by_uid("arxiv:2610.10002")["venue"] == "NeurIPS"
    src = db.get_source(sid)
    assert db.source_counts()[sid] == {"new": 2}
    assert {p["uid"] for p in db.papers_by_status("new", source=src)} == {"arxiv:2610.10001", "arxiv:2610.10002"}


def _client():
    from paperagent import create_app

    c = create_app(start_scheduler=False).test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    return c


def _csrf(c):
    import re

    return re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").get_data(as_text=True)).group(1)


def test_settings_adds_neurips_even_when_openalex_is_down(fresh_db):
    c = _client()
    token = _csrf(c)
    c.post("/settings/sources/journal/search", data={"_csrf": token, "name": "NeurIPS"})  # offline in tests
    html = c.get("/settings").get_data(as_text=True)
    assert "OpenAlex에 연결하지 못해" in html and "Neural Information Processing Systems" in html
    resp = c.post("/settings/sources/journal", data={"_csrf": token, "key": "neurips", "name": "NeurIPS"}, follow_redirects=True)
    assert "학회 &#39;NeurIPS&#39;을(를) 추가했습니다" in resp.get_data(as_text=True)
    src = next(s for s in db.list_sources(kind="journal") if s["name"] == "NeurIPS")
    assert src["value"] == "venue:neurips" and src["meta"]["type"] == "conference" and "NeurIPS" in src["meta"]["arxiv_terms"]
    # adding it again under another spelling is caught
    resp = c.post("/settings/sources/journal", data={"_csrf": token, "name": "neurips 2025"}, follow_redirects=True)
    assert "이미 등록되어 있습니다" in resp.get_data(as_text=True)


def test_unknown_name_can_be_added_for_later_lookup(fresh_db):
    c = _client()
    token = _csrf(c)
    c.post("/settings/sources/journal", data={"_csrf": token, "name": "Journal of Imaginary Results"})
    assert any(s["value"] == "name:Journal of Imaginary Results" for s in db.list_sources(kind="journal"))


def test_home_and_queue_filter_by_source(fresh_db):
    cl = next(s for s in db.list_sources(kind="arxiv") if s["value"] == "cs.CL")
    nips = db.get_source(db.add_source("journal", "venue:neurips", "NeurIPS", {"type": "conference"}))
    rows = [("arxiv:1", "From CL", cl["id"], None, "arXiv · cs.CL"), ("arxiv:2", "From NeurIPS", nips["id"], "NeurIPS", "NeurIPS"),
            ("arxiv:3", "CL paper later seen at NeurIPS", cl["id"], "NeurIPS", "arXiv · cs.CL"),
            ("arxiv:4", "Old CL paper", None, None, "arXiv · cs.CL")]  # collected before sources were recorded
    for uid, title, sid, venue, label in rows:
        db.insert_paper({"uid": uid, "source_kind": "arxiv", "source_name": label, "title": title, "source_id": sid,
                         "venue": venue, "abstract": f"Abstract of {title}"})
        db.update_paper(db.get_paper_by_uid(uid)["id"], status="done", score_overall=7)
    c = _client()
    html = c.get(f"/?source={nips['id']}").get_data(as_text=True)
    assert "From NeurIPS" in html and "CL paper later seen at NeurIPS" in html and "From CL" not in html
    assert "출처별 논문" in html and "모든 출처 보기" in html
    html = c.get(f"/?source={cl['id']}").get_data(as_text=True)
    assert "From CL" in html and "Old CL paper" in html and "From NeurIPS" not in html
    for uid in ("arxiv:1", "arxiv:2"):
        db.update_paper(db.get_paper_by_uid(uid)["id"], status="screened", relevance=8)
    html = c.get(f"/queue?source={nips['id']}").get_data(as_text=True)
    assert "From NeurIPS" in html and "From CL" not in html and "초록 보기" in html and "Abstract of From NeurIPS" in html
    assert c.get(f"/settings").status_code == 200


def test_openalex_outage_is_retried_next_run(fresh_db):
    sid = db.add_source("journal", "name:CVPR", "CVPR")
    say = []
    with mock.patch.object(venues.openalex, "search_sources", side_effect=ConnectionError("down")):
        pipeline.resolve_journals(say.append)
    assert not (db.get_source(sid)["meta"] or {}).get("openalex_checked")
    hit = [{"id": "S99", "name": "IEEE/CVF Conference on Computer Vision and Pattern Recognition", "works_count": 9000}]
    with mock.patch.object(venues.openalex, "search_sources", return_value=hit):
        pipeline.resolve_journals(say.append)
    meta = db.get_source(sid)["meta"]
    assert meta["openalex_id"] == "S99" and meta["type"] == "conference" and "CVPR" in meta["arxiv_terms"]
