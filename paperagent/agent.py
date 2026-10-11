"""The Claude reviewer agent.

Two steps per run:
  1. screen()  - cheap pass over titles+abstracts: how relevant is each paper to
                 the user's keywords? (batched, structured JSON output)
  2. review()  - full read of the PDF (sent to Claude as a document block, so it
                 sees text, tables and figures), scoring and a Korean summary plus
                 the copy for the Instagram card news.
"""
import base64
import json
import logging

import anthropic

from . import config, pdfs

log = logging.getLogger(__name__)

PDF_LIMIT_BYTES = 30 * 1024 * 1024  # API request limit is 32MB incl. base64 overhead
PDF_LIMIT_PAGES = 600

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4, timeout=900)
    return _client


class ReviewError(RuntimeError):
    pass


def _call(model: str, system: str, content, schema: dict, effort: str, max_tokens: int) -> dict:
    """Streamed request with a JSON-schema constrained answer."""
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    if config.CLAUDE_FALLBACKS:
        # On a safety-classifier refusal, the API re-runs the request on
        # Anthropic's recommended fallback model instead of failing.
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    with client().beta.messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        cat = getattr(msg.stop_details, "category", None) if msg.stop_details else None
        raise ReviewError(f"model declined the request (category={cat})")
    if msg.stop_reason == "max_tokens":
        raise ReviewError("response truncated at max_tokens")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if not text:
        raise ReviewError(f"no text in response (stop_reason={msg.stop_reason})")
    return json.loads(text)


# ---------------------------------------------------------------------------
# 1. Screening
# ---------------------------------------------------------------------------

SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "relevance": {"type": "number", "description": "0-10"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "relevance", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

SCREEN_SYSTEM = """You triage newly published research papers for a reader who follows AI / computer-science research.
For every paper, rate how relevant and worth reading it is for the reader's interest keywords on a 0-10 scale:
10 = squarely on a keyword and likely significant; 6 = clearly related and worth a full read;
3 = only tangentially related; 0 = unrelated.
Penalise incremental or purely application papers slightly; reward new methods, strong empirical results and broadly useful findings.
Write `reason` as one short Korean sentence."""


def screen(papers, keywords, batch_size: int = 20):
    """Returns {paper_id: (relevance, reason)}."""
    out = {}
    for i in range(0, len(papers), batch_size):
        batch = papers[i : i + batch_size]
        listing = "\n\n".join(
            f"[{j}] {p['title']}\nSource: {p.get('source_name') or p['source_kind']}\nAbstract: {(p.get('abstract') or '')[:1800]}"
            for j, p in enumerate(batch)
        )
        prompt = (
            f"Interest keywords: {', '.join(keywords)}\n\n"
            f"Rate each of the {len(batch)} papers below (use the bracketed index).\n\n{listing}"
        )
        data = _call(config.SCREEN_MODEL, SCREEN_SYSTEM, prompt, SCREEN_SCHEMA, effort="low", max_tokens=8000)
        for r in data["results"]:
            if 0 <= r["index"] < len(batch):
                rel = max(0.0, min(10.0, float(r["relevance"])))
                out[batch[r["index"]]["id"]] = (rel, r["reason"])
    return out


# ---------------------------------------------------------------------------
# 2. Full review
# ---------------------------------------------------------------------------

_str = {"type": "string"}
_str_list = {"type": "array", "items": {"type": "string"}}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "title_ko": {"type": "string", "description": "Natural Korean translation of the title"},
        "one_liner": {"type": "string", "description": "One-sentence Korean takeaway (<= 60 chars)"},
        "tldr": {"type": "string", "description": "3-4 sentence Korean summary"},
        "problem": _str,
        "method": _str,
        "key_results": {**_str_list, "description": "3-5 concrete results with numbers where available"},
        "contributions": _str_list,
        "limitations": _str_list,
        "why_it_matters": _str,
        "who_should_read": _str,
        "tags": {**_str_list, "description": "3-6 short Korean or English topic tags"},
        "methodology_score": {"type": "number", "description": "1-10"},
        "methodology_reason": _str,
        "novelty_score": {"type": "number", "description": "1-10"},
        "novelty_reason": _str,
        "read_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "card": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Short field tag, e.g. 'LLM 에이전트'"},
                "cover_headline": {"type": "string", "description": "Korean hook, 2 lines separated by \\n, <= 16 chars per line"},
                "cover_sub": {"type": "string", "description": "One Korean sentence under the headline"},
                "slides": {
                    "type": "array",
                    "description": "Exactly 3 slides: what problem / how it works / what results",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "2-6 char section label, e.g. '문제', '방법', '결과'"},
                            "headline": {"type": "string", "description": "2 lines separated by \\n, <= 16 chars per line"},
                            "body": {"type": "string", "description": "2 short Korean sentences separated by \\n, <= 34 chars each"},
                            "figure_number": {"type": "integer", "description": "Figure to show from the extracted list, 0 for none"},
                            "highlight": {"type": "string", "description": "Optional big stat, e.g. '+12.4%p' or '3.2x', else empty"},
                            "highlight_label": {"type": "string", "description": "What the stat measures, else empty"},
                        },
                        "required": ["label", "headline", "body", "figure_number", "highlight", "highlight_label"],
                        "additionalProperties": False,
                    },
                },
                "summary_rows": {
                    "type": "array",
                    "description": "Exactly 5 rows for the summary table slide",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "e.g. 문제 / 방법 / 데이터 / 결과 / 한계"},
                            "sub": {"type": "string", "description": "tiny sub-label"},
                            "value": {"type": "string", "description": "<= 24 chars"},
                            "value_sub": {"type": "string", "description": "<= 32 chars detail"},
                        },
                        "required": ["label", "sub", "value", "value_sub"],
                        "additionalProperties": False,
                    },
                },
                "caption": {"type": "string", "description": "Instagram caption in Korean, 4-8 lines, ends with 5-8 hashtags"},
            },
            "required": ["category", "cover_headline", "cover_sub", "slides", "summary_rows", "caption"],
            "additionalProperties": False,
        },
    },
    "required": [
        "title_ko", "one_liner", "tldr", "problem", "method", "key_results", "contributions", "limitations",
        "why_it_matters", "who_should_read", "tags", "methodology_score", "methodology_reason",
        "novelty_score", "novelty_reason", "read_confidence", "card",
    ],
    "additionalProperties": False,
}

REVIEW_SYSTEM = """You are a senior AI/CS researcher who reads new papers in full and writes sharp, honest reviews in Korean for a Korean audience of engineers and researchers.

Read the whole paper - method, experiments, tables, figures and appendix - before judging. Base every claim on what the paper actually shows; quote concrete numbers (datasets, baselines, metrics, deltas) rather than adjectives. If something is unclear or missing, say so.

Scoring (1-10, decimals allowed, be calibrated - most papers land 5-7):
- methodology_score - soundness and trustworthiness: appropriate baselines and ablations, statistical care, dataset/contamination issues, reproducibility (code, details), whether the evidence supports the claims.
  9-10 rigorous and convincing; 7-8 solid with minor gaps; 5-6 typical, some missing controls; 3-4 notable flaws; 1-2 claims unsupported.
- novelty_score - originality and contribution: new idea or insight vs. incremental tuning, size of the advance over prior work, likely influence.
  9-10 likely field-shaping; 7-8 clearly new and useful; 5-6 solid incremental; 3-4 minor variation; 1-2 nothing new.
Explain each score in 1-2 Korean sentences.

The `card` object is copy for a 6-slide Instagram card-news post. Write punchy, plain Korean a non-specialist can follow, keep technical terms in English where natural (e.g. "Transformer", "RLHF"). Respect the length limits - text is rendered at large size. Slides go: 1 problem/motivation, 2 how the method works, 3 key results. For each slide pick the most informative figure from the extracted figure list (by number) - prefer the method overview for slide 2 and a results plot/table for slide 3. Use 0 only if no listed figure fits. Never invent figure numbers that are not in the list.

All prose fields are in Korean."""


def _document_block(pdf_path):
    size = pdf_path.stat().st_size
    pages = pdfs.page_count(pdf_path)
    if size <= PDF_LIMIT_BYTES and pages <= PDF_LIMIT_PAGES:
        data = base64.standard_b64encode(pdf_path.read_bytes()).decode()
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data},
                "title": pdf_path.stem}, "full_pdf"
    # Too big to attach as a PDF: send the extracted text (no figures) instead.
    text = pdfs.pdf_text(pdf_path)
    return {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text},
            "title": pdf_path.stem}, "full_text"


def review(paper: dict, keywords, pdf_path=None, figures=()):
    """Full review. Returns (review_dict, read_depth)."""
    content = []
    if pdf_path:
        block, depth = _document_block(pdf_path)
        content.append(block)
    else:
        depth = "abstract"
    fig_list = "\n".join(f"- Figure {f['number']} (p.{f['page']}): {f['caption'][:220]}" for f in figures) or "(none extracted)"
    meta = (
        f"Title: {paper['title']}\n"
        f"Authors: {', '.join(paper.get('authors', [])[:12])}\n"
        f"Venue/source: {paper.get('source_name') or paper['source_kind']}\n"
        f"Published: {paper.get('published')}\n"
        f"Reader's interest keywords: {', '.join(keywords)}\n"
    )
    if depth == "abstract":
        meta += (
            f"\nOnly the abstract is available (no open-access PDF):\n{paper.get('abstract')}\n"
            "Set read_confidence to 'low' and keep scores conservative.\n"
        )
    content.append(
        {
            "type": "text",
            "text": f"{meta}\nFigures extracted from the PDF that can be shown on the cards:\n{fig_list}\n\n"
            "Review this paper and fill in every field.",
        }
    )
    data = _call(config.CLAUDE_MODEL, REVIEW_SYSTEM, content, REVIEW_SCHEMA, effort=config.CLAUDE_EFFORT, max_tokens=32000)
    for k in ("methodology_score", "novelty_score"):
        data[k] = round(max(1.0, min(10.0, float(data[k]))), 1)
    data["overall_score"] = round((data["methodology_score"] + data["novelty_score"]) / 2, 1)
    valid = {f["number"] for f in figures}
    for s in data["card"]["slides"]:
        if s["figure_number"] not in valid:
            s["figure_number"] = 0
    return data, depth
