"""Force-stopping a run and following its progress live."""
import re
import threading
from unittest import mock

from paperagent import agent, create_app, db, pipeline


def _paper(uid, rel=8.0):
    db.insert_paper({"uid": uid, "source_kind": "arxiv", "source_name": "arXiv · cs.CL", "title": f"Paper {uid}",
                     "abstract": "An AI agent.", "matched_keywords": ["AI agent"]})
    pid = db.get_paper_by_uid(uid)["id"]
    db.update_paper(pid, status="screened", relevance=rel)
    return pid


def _admin():
    c = create_app(start_scheduler=False).test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    token = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").get_data(as_text=True)).group(1)
    return c, token


def test_force_stop_frees_a_stuck_run(fresh_db):
    a, b = _paper("arxiv:a", 9), _paper("arxiv:b", 8)
    stuck, release = threading.Event(), threading.Event()

    def hanging_review(paper, keywords, pdf_path=None, figures=()):
        agent._notify("judge", "claude-opus-5-5")  # the console shows the step it is on
        stuck.set()
        release.wait(10)  # a Claude call that never comes back (until we let it)
        agent._notify("write", "claude-sonnet-5-5")  # the next step notices the stop
        raise AssertionError("should have been cancelled")

    with mock.patch.object(pipeline, "collect", return_value=0), mock.patch.object(pipeline, "resolve_journals"), \
         mock.patch.object(pipeline, "screen_new", return_value=0), mock.patch.object(pipeline.trending, "refresh_all") as trends, \
         mock.patch.object(pipeline.pdfs, "download_pdf", side_effect=ValueError("no pdf")), \
         mock.patch.object(agent, "review", side_effect=hanging_review):
        t = threading.Thread(target=pipeline.run, args=("test",))
        t.start()
        try:
            assert stuck.wait(10)
            run = db.active_run()
            visitor = create_app(start_scheduler=False).test_client()
            visitor.environ_base["REMOTE_ADDR"] = "10.0.0.9"
            live = visitor.get("/api/run/live").get_json()
            assert live["running"] and live["run"]["progress"]["phase"] == "read"
            assert live["run"]["progress"]["stage"] == "평가 중" and live["run"]["progress"]["current"] == a
            assert [x["id"] for x in live["run"]["progress"]["targets"]] == [a, b]
            assert "log" not in live["run"]  # visitors get progress only

            c, token = _admin()
            assert c.get("/api/run/live").get_json()["run"]["log"]  # the admin sees the console log
            r = c.post("/run/stop", data={"_csrf": token}, headers={"Accept": "application/json"})
            assert r.get_json() == {"stopped": True}
            # immediately: nothing is running, the paper is back in the queue, a new run could start
            assert not pipeline.is_running() and db.get_paper(a)["status"] == "screened"
            assert db.get_run(run["id"])["status"] == "aborted"
        finally:
            release.set()  # the stuck call finally returns...
            t.join(10)
    assert not t.is_alive()
    run = db.get_run(run["id"])
    assert run["status"] == "aborted" and "강제 종료" in run["log"]  # ...and changes nothing
    assert db.get_paper(a)["status"] == "screened" and db.get_paper(b)["status"] == "screened"
    trends.assert_not_called()


def test_stop_without_a_run(fresh_db):
    c, token = _admin()
    resp = c.post("/run/stop", data={"_csrf": token}, follow_redirects=True)
    assert "실행 중인 작업이 없습니다" in resp.get_data(as_text=True)


def test_progress_through_a_whole_run(fresh_db):
    a = _paper("arxiv:a")
    seen = []

    def review(paper, keywords, pdf_path=None, figures=()):
        for stage in ("read", "judge", "write", "card"):
            agent._notify(stage, "m")
            seen.append(db.get_run(db.active_run()["id"])["progress"]["stage"])
        raise agent.ReviewError("stop here")

    with mock.patch.object(pipeline, "collect", return_value=0), mock.patch.object(pipeline, "resolve_journals"), \
         mock.patch.object(pipeline, "screen_new", return_value=0), mock.patch.object(pipeline.trending, "refresh_all"), \
         mock.patch.object(pipeline.pdfs, "download_pdf", side_effect=ValueError("no pdf")), \
         mock.patch.object(agent, "review", side_effect=review):
        run_id = pipeline.run("test")
    assert seen == ["원문 읽는 중", "평가 중", "리뷰 작성 중", "카드 문구 작성 중"]
    progress = db.get_run(run_id)["progress"]
    assert progress["phase"] == "done" and progress["failed"] == [a] and progress["done"] == []


def test_queue_shows_the_reading_plan(fresh_db):
    db.set_setting("max_full_reads", 2)
    ids = [_paper(f"arxiv:{i}", rel) for i, rel in enumerate([9.5, 9.0, 8.5, 8.0])]
    db.queue_papers([ids[3]])  # hand-picked: read first
    c, token = _admin()
    html = c.get("/queue").get_data(as_text=True)
    plan = html[html.index('id="plan"'):html.index("</section>", html.index('id="plan"'))]
    order = [plan.index(f"Paper arxiv:{i}") for i in (3, 0, 1)]
    assert order == sorted(order) and "Paper arxiv:2" not in plan  # 1 picked + top 2, in reading order
    assert "Paper arxiv:2" in html  # the rest waits below, still visible
    # '읽지 않기' takes a paper out of the plan, and the threshold sync does not put it back
    c.post("/queue/read", data={"_csrf": token, "paper_id": str(ids[0]), "when": "skip"})
    assert db.get_paper(ids[0])["status"] == "skipped"
    html = c.get("/queue").get_data(as_text=True)
    plan = html[html.index('id="plan"'):html.index("</section>", html.index('id="plan"'))]
    assert "Paper arxiv:0" not in plan and "Paper arxiv:2" in plan and "직접 제외" in html
    assert db.get_paper(ids[0])["status"] == "skipped"


def test_queue_shows_the_running_runs_list(fresh_db):
    a, b = _paper("arxiv:a"), _paper("arxiv:b")
    run_id = db.start_run("test")
    db.set_run_progress(run_id, {"phase": "read", "targets": [{"id": a, "title": "A"}, {"id": b, "title": "B"}],
                                 "done": [a], "failed": [], "current": b, "stage": "평가 중"})
    c, _ = _admin()
    html = c.get("/queue").get_data(as_text=True)
    assert "이번 실행에서 읽는 논문" in html and "지금 실행 중" in html and "강제 종료" in html
    assert '<li class="done">' in html and '<li class="now">' in html
