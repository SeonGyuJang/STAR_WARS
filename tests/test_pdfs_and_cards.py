import copy
import re
from pathlib import Path

from PIL import Image

from paperagent import cardnews, demo, pdfs


def test_safe_filename():
    assert pdfs.safe_filename('A/B: "C"?') == "A B C.pdf"
    assert pdfs.safe_filename("x" * 300).endswith(".pdf")
    assert len(pdfs.safe_filename("x" * 300)) <= 154


def test_extract_figures(sample_pdf, tmp_path):
    figs = pdfs.extract_figures(sample_pdf, tmp_path)
    assert [f["number"] for f in figs] == [1, 2]
    assert figs[0]["caption"].startswith("Figure 1:")
    assert figs[1]["page"] == 2
    w, h = Image.open(figs[0]["path"]).size
    assert w > 1000 and 200 < h < 400  # the whole 4-box diagram at 2.5x, not the page
    w, h = Image.open(figs[1]["path"]).size
    assert 900 < w < 1300 and 500 < h < 700  # the chart plus its axis labels and legend
    assert (tmp_path / "page1.png").exists()


def test_render_cards(sample_pdf, tmp_path):
    figs = pdfs.extract_figures(sample_pdf, tmp_path / "figs")
    review = copy.deepcopy(demo.SAMPLE_REVIEW)
    paper = {"uid": "arxiv:2610.01234", "title": "Self-Verification Loops", "authors": ["A. Kim"],
             "source_kind": "arxiv", "source_name": "arXiv · cs.CL", "published": "2026-10-09", "url": "https://arxiv.org/abs/2610.01234"}
    paths = cardnews.render(paper, review, figs, tmp_path / "cards")
    assert len(paths) == 10  # cover .. so-what, verdict, summary, outro
    for p in paths:
        assert Image.open(p).size == (1080, 1350)
    caption = (tmp_path / "cards" / "caption.txt").read_text()
    assert "https://arxiv.org/abs/2610.01234" in caption and "7.8/10" in caption


def test_old_card_format_still_renders(sample_pdf, tmp_path):
    """Reviews written before the current layout keep working with "카드 다시 만들기"."""
    figs = pdfs.extract_figures(sample_pdf, tmp_path / "figs")
    review = copy.deepcopy(demo.SAMPLE_REVIEW)
    card = review["card"]
    review["card"] = {k: card[k] for k in ("category", "cover_headline", "cover_sub", "summary_rows", "caption")}
    review["card"]["slides"] = [
        {"label": "문제", "headline": "왜\n어려울까", "body": "첫 줄\n둘째 줄", "figure_number": 0, "highlight": "", "highlight_label": ""},
        {"label": "방법", "headline": "이렇게\n풉니다", "body": "방법 설명", "figure_number": 1, "highlight": "", "highlight_label": ""},
        {"label": "결과", "headline": "좋아\n졌습니다", "body": "결과", "figure_number": 2, "highlight": "+12.4%p", "highlight_label": "MATH"},
    ]
    for k in ("key_points", "method_steps", "setup"):
        review.pop(k)
    review["key_results"] = ["MATH 41.2% → 53.6%"]
    norm = cardnews.normalize_card(review)
    assert norm["problem"]["points"] == ["첫 줄", "둘째 줄"] and norm["results"]["highlight"] == "+12.4%p"
    paper = {"uid": "arxiv:2610.01234", "title": "Self-Verification Loops", "authors": ["A. Kim"],
             "source_kind": "arxiv", "published": "2026-10-09", "url": "https://arxiv.org/abs/2610.01234"}
    paths = cardnews.render(paper, review, figs, tmp_path / "cards")
    assert len(paths) == 7  # no steps / setup slides for the old format
    assert all(Image.open(p).size == (1080, 1350) for p in paths)


def test_hashtags_follow_instagram_limit():
    card = {"caption": "본문\n#AI #딥러닝 #머신러닝 #개발자 #공부", "hashtags": ["#LLM", "AI", "#컴퓨터 비전", "#추가"]}
    tags = cardnews.hashtags(card)
    assert tags == ["#인공지능", "#AI", "#논문리뷰", "#LLM", "#컴퓨터비전"]  # fixed tags first, no duplicates, max 5
    # older reviews kept their tags inside the caption; they are moved to the end and capped too
    old = {"caption": "본문입니다\n\n#AI #LLM #논문리뷰 #머신러닝 #딥러닝 #STARWARS"}
    review = {"card": old, "overall_score": 7.8, "methodology_score": 7.5, "novelty_score": 8.0}
    caption = cardnews.build_caption({"title": "T", "url": "https://x"}, review)
    assert caption.startswith("본문입니다") and caption.count("#") == 5
    assert caption.splitlines()[-1] == "#인공지능 #AI #논문리뷰 #LLM #머신러닝"


def test_hard_words_are_explained_once(sample_pdf, tmp_path):
    figs = pdfs.extract_figures(sample_pdf, tmp_path / "figs")
    paper = {"uid": "arxiv:2610.01234", "title": "T", "authors": ["A"], "source_kind": "arxiv", "url": "https://x"}
    html = cardnews.build_html(paper, copy.deepcopy(demo.SAMPLE_REVIEW), figs)
    assert html.count('<b>MATH</b>') == 1  # used on several cards, explained on the first only
    assert 'MATH<sup class="star">*</sup>' in html


def test_card_text_and_figure_captions(sample_pdf, tmp_path):
    figs = pdfs.extract_figures(sample_pdf, tmp_path / "figs")
    review = copy.deepcopy(demo.SAMPLE_REVIEW)
    html = cardnews.build_html({"uid": "arxiv:1", "title": "T", "url": "https://x"}, review, figs)
    assert "작은 AI는 왜<br>수학 풀이에 약할까요?" in html  # the writer's line break is kept
    assert '<div class="cap">Figure 1. Overview' in html  # "Figure 1." stays together on one line
    assert "그래서 나랑 무슨 상관?" in html and 'class="hbar on"' in html  # so-what card, bars to scale
    assert "page1" not in html  # no first-page screenshot as a picture
    outro = html[html.index('class="slide outro"'):]
    assert re.search(r"읽고,</i></span><br><span", outro) and "<b>@starwars.research</b></p>" in outro


def test_closing_slide_video(sample_pdf, tmp_path):
    """Only the closing slide is a video: its words rise into place; every other slide is an image."""
    import subprocess

    import pytest

    from paperagent import video

    if not video.available():
        pytest.skip("ffmpeg not available")
    figs = pdfs.extract_figures(sample_pdf, tmp_path / "figs")
    paper = {"uid": "arxiv:1", "title": "T", "url": "https://x"}
    paths, clips = cardnews.capture(cardnews.build_html(paper, copy.deepcopy(demo.SAMPLE_REVIEW), figs), tmp_path / "out")
    assert list(clips) == [len(paths)] == [10]
    info = subprocess.run([video.ffmpeg_exe(), "-i", clips[10]], capture_output=True, text=True).stderr
    assert "h264" in info and "1080x1350" in info and "yuv420p" in info and "Duration: 00:00:07" in info
    frames = []
    for t in ("0.1", "5.5"):
        out = tmp_path / f"f{t}.png"
        subprocess.run([video.ffmpeg_exe(), "-v", "error", "-ss", t, "-i", clips[10], "-frames:v", "1", "-y", str(out)], check=True)
        frames.append(Image.open(out).convert("L"))
    claim = [f.crop((60, 600, 1020, 900)) for f in frames]
    assert claim[0].getextrema()[1] < 80  # the claim is not there yet
    initials = frames[0].crop((72, 180, 140, 520))  # ...but S T A R stand from the first frame
    meanings = frames[0].crop((150, 180, 500, 520))  # while their meanings have not unfolded yet
    assert initials.getextrema()[1] > 200 and meanings.getextrema()[1] < 140
    # the last frame is the still card
    still = Image.open(paths[-1]).convert("L")
    from PIL import ImageChops, ImageStat

    assert ImageStat.Stat(ImageChops.difference(frames[1], still)).mean[0] < 3


def test_figures_never_touch_the_footnotes(sample_pdf, tmp_path):
    """Crowded slides (long footnotes next to a figure): the figure keeps clear of the notes and nothing spills out."""
    from playwright.sync_api import sync_playwright

    figs = pdfs.extract_figures(sample_pdf, tmp_path / "figs")
    review = copy.deepcopy(demo.SAMPLE_REVIEW)
    card = review["card"]
    card["terms"] = card["terms"] + [{"term": "Self-Consistency", "meaning": "같은 질문에 답을 여러 번 만든 뒤 가장 많이 나온 답을 고르는 방식"}]
    card["results"]["highlight_label"] = "MATH* 정답률 상승, 파라미터* 7B, Self-Consistency* 대비"
    card["problem"]["figure_number"], card["results"]["figure_number"] = 2, 0
    card["problem"]["points"] = [p + " 파라미터* 기준으로도 같습니다." for p in card["problem"]["points"]]
    path = tmp_path / "cards.html"
    path.write_text(cardnews.build_html({"uid": "arxiv:1", "title": "T", "url": "https://x"}, review, figs), encoding="utf-8")
    with sync_playwright() as p:
        browser = cardnews._launch(p)
        page = browser.new_page(viewport={"width": 1080, "height": 1350})
        page.goto(path.as_uri())
        page.wait_for_selector("body[data-ready='1']")
        rows = page.evaluate("""() => [...document.querySelectorAll('.slide')].map((s) => {
            const r = (e) => e && e.getBoundingClientRect(), f = s.querySelector('.figure'), m = s.querySelector('.main');
            const next = f && f.nextElementSibling, line = s.querySelector('.bottom'), ft = s.querySelector('.foot');
            const shown = m ? [...m.children].filter((c) => r(c).height > 0) : [];
            const prev = ft && ft.previousElementSibling;
            return {gap: f && next ? r(next).top - r(f).bottom : null,
                    spill: m ? Math.max(0, ...shown.map((c) => r(c).bottom)) - r(m).bottom : 0,
                    toLine: m && line ? r(line).top - Math.max(0, ...shown.map((c) => r(c).bottom)) : null,
                    aboveNotes: ft && prev ? r(ft).top - r(prev).bottom : null};
        })""")
        browser.close()
    assert any(r["gap"] is not None for r in rows)
    assert all(r["gap"] is None or r["gap"] >= 18 for r in rows), rows
    assert all(r["spill"] <= 1 for r in rows), rows
    # nothing touches the line above the source/account, and the notes keep clear of what is above them
    assert all(r["toLine"] is None or r["toLine"] >= 28 for r in rows), rows
    assert all(r["aboveNotes"] is None or r["aboveNotes"] >= 28 for r in rows), rows
