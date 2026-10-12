"""PDFs uploaded from the settings page: rated, read in full, reviewed, carded."""
import io
import re
from unittest import mock

from paperagent import agent, create_app, db, pipeline, storage


def _admin():
    c = create_app(start_scheduler=False).test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    token = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").get_data(as_text=True)).group(1)
    return c, token


def test_upload_creates_paper_and_starts_reading(fresh_db, sample_pdf):
    c, token = _admin()
    assert "PDF로 논문 추가" in c.get("/settings").get_data(as_text=True)
    with mock.patch.object(pipeline, "run_in_background", return_value=True) as bg:
        r = c.post("/settings/upload", data={"_csrf": token, "pdf": (io.BytesIO(sample_pdf.read_bytes()), "paper.pdf")},
                   content_type="multipart/form-data")
    p = db.query_papers(status="new")[0] if db.query_papers(status="new") else None
    assert p and p["source_kind"] == "upload" and p["source_name"] == "직접 업로드"
    assert p["title"] == "Self-Verification Loops Make Small Language Models Better Reasoners"
    assert p["priority"] == 1 and p["pdf_url"].startswith("storage:uploads/")
    assert storage.get(p["pdf_url"][len("storage:"):])[0].startswith(b"%PDF")
    bg.assert_called_once_with("manual", paper_ids=[p["id"]])
    assert r.headers["Location"].endswith(f"/paper/{p['id']}")
    page = c.get(f"/paper/{p['id']}").get_data(as_text=True)
    assert "업로드한 PDF · 읽을 차례입니다" in page and f"/files/upload/{p['id']}.pdf" in page
    assert c.get(f"/files/upload/{p['id']}.pdf").data.startswith(b"%PDF")
    visitor = create_app(start_scheduler=False).test_client()
    visitor.environ_base["REMOTE_ADDR"] = "10.0.0.9"
    assert visitor.get(f"/files/upload/{p['id']}.pdf").status_code == 302  # admin only

    # the same file again is not duplicated; it goes back to the front of the queue
    with mock.patch.object(pipeline, "run_in_background", return_value=True):
        c.post("/settings/upload", data={"_csrf": token, "pdf": (io.BytesIO(sample_pdf.read_bytes()), "again.pdf")},
               content_type="multipart/form-data")
    assert len([x for x in db.papers_by_status("screened") + db.papers_by_status("new") if x["source_kind"] == "upload"]) == 1


def test_upload_rejects_non_pdf(fresh_db):
    c, token = _admin()
    r = c.post("/settings/upload", data={"_csrf": token, "pdf": (io.BytesIO(b"hello"), "notes.txt")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "PDF 파일이 아닙니다" in r.get_data(as_text=True)
    assert not db.query_papers(status="new")


def test_uploaded_paper_is_rated_then_read_even_if_low(fresh_db, sample_pdf):
    pid, new = pipeline.add_uploaded_pdf(sample_pdf.read_bytes(), "paper.pdf", "My Uploaded Paper")
    assert new and db.get_paper(pid)["title"] == "My Uploaded Paper"
    key = db.get_paper(pid)["pdf_url"][len("storage:"):]
    read = []

    def process(paper, keywords, say):
        read.append((paper["id"], paper["relevance"]))
        return True, 0.1

    with mock.patch.object(agent, "screen", return_value={pid: (2.0, "관심 키워드와 거리가 멉니다")}), \
         mock.patch.object(pipeline.trending, "refresh_all"), \
         mock.patch.object(pipeline, "process_paper", side_effect=process):
        pipeline.run("manual", paper_ids=[pid])
    assert read == [(pid, 2.0)]  # rated 2/10, read anyway because it was uploaded
    assert db.get_paper(pid)["relevance_reason"] == "관심 키워드와 거리가 멉니다"
    assert storage.get(key) is not None  # kept until the review succeeds


def test_uploaded_pdf_is_deleted_after_review(fresh_db, sample_pdf):
    from paperagent import demo

    pid, _ = pipeline.add_uploaded_pdf(sample_pdf.read_bytes(), "paper.pdf")
    paper = db.get_paper(pid)
    key = paper["pdf_url"][len("storage:"):]
    review = dict(demo.SAMPLE_REVIEW, _cost={"total_usd": 0.1, "calls": []})
    with mock.patch.object(agent, "review", return_value=(review, "full_pdf")) as rv, \
         mock.patch.object(pipeline.cardnews, "render_and_store", return_value=[]):
        ok, _ = pipeline.process_paper(paper, ["AI agent"])
    assert ok and rv.call_args.args[2] is not None  # Claude got the uploaded PDF
    assert storage.get(key) is None and db.get_paper(pid)["pdf_url"] is None and db.get_paper(pid)["status"] == "done"
