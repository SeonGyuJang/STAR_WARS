import pytest
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


ARXIV_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel><title>cs.CL updates on arXiv.org</title>
<item><title>Self-Verification for Large Language Models</title><link>https://arxiv.org/abs/2610.01234</link>
<description>arXiv:2610.01234v1 Announce Type: new
Abstract: We study large language model reasoning with a verification loop.</description>
<guid isPermaLink="false">oai:arXiv.org:2610.01234v1</guid><category>cs.CL</category><category>cs.AI</category>
<pubDate>Fri, 16 Oct 2026 00:00:00 -0400</pubDate><dc:creator>Ana Kim, Ben Lee</dc:creator></item>
<item><title>An old paper, revised</title><link>https://arxiv.org/abs/2401.00001</link>
<description>arXiv:2401.00001v3 Announce Type: replace
Abstract: Large language model things.</description><category>cs.CL</category>
<pubDate>Fri, 16 Oct 2026 00:00:00 -0400</pubDate><dc:creator>X</dc:creator></item>
</channel></rss>"""


def test_arxiv_rss_feed():
    papers = arxiv.parse_rss(ARXIV_RSS)
    assert [p["uid"] for p in papers] == ["arxiv:2610.01234"]  # replacements of old papers left out
    p = papers[0]
    assert p["abstract"].startswith("We study large language model") and p["authors"] == ["Ana Kim", "Ben Lee"]
    assert p["categories"] == ["cs.CL", "cs.AI"] and p["pdf_url"] == "https://arxiv.org/pdf/2610.01234"


def test_arxiv_busy_is_retried_after_the_wait_it_asks_for():
    import requests

    def busy(status, after=None):
        resp = mock.Mock(status_code=status, headers={"Retry-After": after} if after else {})
        return requests.HTTPError(f"{status}", response=resp)

    ok = mock.Mock(text="<feed></feed>")
    with mock.patch.object(arxiv, "http_get", side_effect=[busy(429, "7"), busy(503), ok]) as get, \
            mock.patch.object(arxiv.time, "sleep") as sleep:
        arxiv.fetch_categories(["cs.CL", "cs.AI"], ["llm"])
    assert get.call_count == 3 and [c.args[0] for c in sleep.call_args_list if c.args[0] > 1] == [7, 45]  # the wait arXiv asked for, then the 2nd default wait
    q = get.call_args.kwargs["params"]["search_query"]
    assert q.startswith("(cat:cs.CL OR cat:cs.AI) AND") and "all:llm" in q  # one request for both categories
    with mock.patch.object(arxiv, "http_get", side_effect=[busy(429)] * 3), mock.patch.object(arxiv.time, "sleep"):
        with pytest.raises(arxiv.ArxivBusy):
            arxiv.fetch_categories(["cs.CL"], ["llm"])


def test_collect_falls_back_to_rss_when_the_api_is_busy(fresh_db):
    from paperagent import pipeline

    log = []
    rss = mock.Mock(text=ARXIV_RSS)
    with mock.patch.object(pipeline.arxiv, "fetch_categories", side_effect=arxiv.ArxivBusy("arXiv API is busy (429)")), \
            mock.patch.object(arxiv, "http_get", return_value=rss), \
            mock.patch.object(pipeline, "resolve_journals"), \
            mock.patch.object(pipeline.db, "list_sources", side_effect=lambda kind=None, enabled_only=False:
                              [{"id": 1, "kind": "arxiv", "value": "cs.CL", "name": "arXiv · cs.CL", "enabled": 1}] if kind == "arxiv" else []):
        new = pipeline.collect(["large language model"], fresh_db.pipeline_settings(), log.append)
    assert new == 1 and fresh_db.get_paper_by_uid("arxiv:2610.01234")["source_id"] == 1
    assert any("RSS" in line for line in log) and not any("RSS failed" in line for line in log)
