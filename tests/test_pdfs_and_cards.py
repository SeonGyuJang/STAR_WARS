import copy

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
    assert len(paths) == 9
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
