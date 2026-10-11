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
    assert len(paths) == 6
    for p in paths:
        assert Image.open(p).size == (1080, 1350)
    caption = (tmp_path / "cards" / "caption.txt").read_text()
    assert "https://arxiv.org/abs/2610.01234" in caption and "7.8/10" in caption
