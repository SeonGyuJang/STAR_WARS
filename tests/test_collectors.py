from datetime import datetime, timezone
from unittest import mock

from conftest import FIXTURES
from paperagent.collectors import arxiv, match_keywords, openalex

KEYWORDS = ["large language model", "AI agent"]


def test_parse_feed_fields():
    papers = arxiv.parse_feed((FIXTURES / "arxiv_feed.xml").read_text())
    assert len(papers) == 3
    p = papers[0]
    assert p["uid"] == "arxiv:2610.01234"
    assert p["title"] == "Self-Verification Loops Make Small Language Models Better Reasoners"
    assert p["authors"] == ["A. Kim", "B. Lee"]
    assert p["pdf_url"] == "https://arxiv.org/pdf/2610.01234v2"
    assert p["doi"] == "10.1234/test.5678"
    assert p["primary_category"] == "cs.CL"
    assert p["categories"] == ["cs.CL", "cs.AI"]


def test_fetch_category_filters_by_date_and_keyword():
    resp = mock.Mock(text=(FIXTURES / "arxiv_feed.xml").read_text())
    with mock.patch.object(arxiv, "http_get", return_value=resp) as get, mock.patch.object(arxiv, "_throttle"):
        found = arxiv.fetch_category("cs.CL", KEYWORDS, lookback_days=3)
    # old paper dropped by date, protein paper dropped by keywords
    assert [p["uid"] for p in found] == ["arxiv:2610.01234"]
    assert set(found[0]["matched_keywords"]) == {"large language model", "AI agent"}
    q = get.call_args.kwargs["params"]["search_query"]
    assert q == 'cat:cs.CL AND (all:"large language model" OR all:"AI agent")'


def test_match_keywords_variants():
    assert match_keywords("Retrieval augmented generation for agents", ["retrieval-augmented generation"]) == [
        "retrieval-augmented generation"
    ]
    assert match_keywords("diffusion models are great", ["diffusion model"]) == ["diffusion model"]
    assert match_keywords("a paper on agentic RL", ["AI agent"]) == []


def test_openalex_work_to_paper():
    work = {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.5555/ABC",
        "display_name": "A Journal Paper",
        "abstract_inverted_index": {"Hello": [0], "world": [1], "again": [2]},
        "authorships": [{"author": {"display_name": "X"}}],
        "publication_date": "2026-10-01",
        "best_oa_location": {"pdf_url": "https://example.org/a.pdf"},
        "primary_location": {"landing_page_url": "https://example.org/a", "source": {"display_name": "JMLR"}},
        "cited_by_count": 4,
    }
    p = openalex.work_to_paper(work)
    assert p["uid"] == "doi:10.5555/abc"
    assert p["abstract"] == "Hello world again"
    assert p["pdf_url"] == "https://example.org/a.pdf"
    assert p["source_name"] == "JMLR"


def test_openalex_falls_back_to_arxiv_pdf():
    work = {
        "id": "https://openalex.org/W9",
        "display_name": "T",
        "locations": [{"landing_page_url": "https://arxiv.org/abs/2401.00001v3"}],
    }
    p = openalex.work_to_paper(work)
    assert p["pdf_url"] == "https://arxiv.org/pdf/2401.00001"
    assert p["uid"] == "openalex:W9"
