"""The Claude reviewer agent, split so each step runs on the cheapest model that
does it well.

  screen()  SCREEN_MODEL (Haiku)   titles + abstracts -> relevance 0-10
  review():
    1. read   READER_MODEL (Haiku)  the whole PDF -> factual English notes.
                                     This is where almost all input tokens go.
    2. judge  JUDGE_MODEL  (Opus)   notes -> methodology / novelty scores.
                                     The decision that matters, on a small input.
    3. write  WRITER_MODEL (Sonnet) notes + verdict -> Korean review and card copy.

Every call's token usage is priced (config.MODEL_PRICES) so the dashboard can
show what each paper and each run actually cost.
"""
import base64
import json
import logging
import os

import anthropic

from . import config, db, pdfs

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


# ---------------------------------------------------------------------------
# API health: problems that need the admin (bad key, no credit, ...) stop the
# AI steps and show a notice; everything that doesn't need Claude keeps working.
# ---------------------------------------------------------------------------

class ApiUnavailable(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def api_key_configured() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return bool(key) and "..." not in key and key != "sk-ant-"


def classify_api_error(exc):
    """(kind, Korean message) for errors an admin has to fix, else None (transient)."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "auth", "API 키가 올바르지 않습니다 (401)."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "permission", "이 API 키로는 해당 모델을 쓸 권한이 없습니다 (403)."
    if isinstance(exc, anthropic.NotFoundError):
        return "model", "설정한 모델 이름을 찾을 수 없습니다 (404). 모델 환경 변수를 확인하세요."
    if isinstance(exc, anthropic.BadRequestError):
        text = str(exc).lower()
        if "credit" in text or "billing" in text or "balance" in text:
            return "billing", "크레딧이 부족하거나 결제 수단이 없습니다."
        return None
    if isinstance(exc, anthropic.APIConnectionError):
        return "connection", "Claude API 서버에 연결할 수 없습니다 (네트워크)."
    if isinstance(exc, anthropic.RateLimitError | anthropic.InternalServerError):
        return None
    if isinstance(exc, anthropic.AnthropicError) and "api_key" in str(exc).lower():
        return "missing_key", "API 키가 설정되지 않았습니다."
    return None


_last_ok = None


def _save_status(value) -> bool:
    try:
        db.set_setting("api_status", value)
        return True
    except Exception as exc:  # bookkeeping must never break a Claude call
        log.warning("could not save API status: %s", exc)
        return False


def record_api_ok():
    global _last_ok
    if _last_ok is not True and _save_status({"ok": True, "checked_at": db.now_iso()}):
        _last_ok = True


def record_api_problem(kind: str, message: str):
    global _last_ok
    _save_status({"ok": False, "kind": kind, "message": message, "checked_at": db.now_iso()})
    _last_ok = False


def api_status() -> dict:
    """Current API state for the dashboard notice."""
    if not api_key_configured():
        return {"ok": False, "kind": "missing_key", "message": "API 키(ANTHROPIC_API_KEY)가 설정되지 않았습니다."}
    return db.get_setting("api_status") or {"ok": True}


def ensure_api():
    """Raise ApiUnavailable early when there is no key at all."""
    if not api_key_configured():
        msg = "API 키(ANTHROPIC_API_KEY)가 설정되지 않았습니다."
        record_api_problem("missing_key", msg)
        raise ApiUnavailable("missing_key", msg)


def check_api() -> dict:
    """Free check (no tokens): look up every configured model."""
    try:
        ensure_api()
        for model in {config.SCREEN_MODEL, config.READER_MODEL, config.JUDGE_MODEL, config.WRITER_MODEL}:
            client().models.retrieve(model)
        record_api_ok()
    except ApiUnavailable:
        pass
    except anthropic.AnthropicError as exc:
        kind, message = classify_api_error(exc) or ("other", f"API 확인 실패: {exc}")
        record_api_problem(kind, message)
    return api_status()


class CostMeter:
    """Accumulates token usage and USD cost across calls."""

    def __init__(self):
        self.calls = []

    def add(self, stage: str, model: str, usage):
        inp = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        out = usage.output_tokens or 0
        self.calls.append({"stage": stage, "model": model, "input_tokens": inp, "output_tokens": out,
                           "usd": round(price(model, inp, out), 5)})

    @property
    def total_usd(self) -> float:
        return round(sum(c["usd"] for c in self.calls), 5)

    def summary(self) -> dict:
        return {"total_usd": self.total_usd, "calls": self.calls}


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = config.MODEL_PRICES.get(model, config.MODEL_PRICES["claude-opus-5-5"])
    if model.startswith("claude-haiku-5-5") and input_tokens > 100_000:
        in_rate, out_rate = config.HAIKU_LONG_PROMPT_PRICES
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def _supports_fallback(model: str) -> bool:
    # Server-side refusal fallback exists for the Opus/Sonnet/Fable 5.x line on
    # the Claude API; Haiku has none.
    return model.startswith(("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5"))


def _call(stage: str, model: str, system: str, content, schema: dict, effort: str, max_tokens: int,
          meter: CostMeter | None = None) -> dict:
    """Streamed request with a JSON-schema constrained answer."""
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    ensure_api()
    try:
        if config.CLAUDE_FALLBACKS and _supports_fallback(model):
            # On a safety-classifier refusal the API re-runs the request on
            # Anthropic's recommended fallback model instead of failing.
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            stream_ctx = client().beta.messages.stream(**kwargs)
        else:
            stream_ctx = client().messages.stream(**kwargs)
        with stream_ctx as stream:
            msg = stream.get_final_message()
    except anthropic.AnthropicError as exc:
        problem = classify_api_error(exc)
        if problem:
            record_api_problem(*problem)
            raise ApiUnavailable(*problem) from exc
        raise
    record_api_ok()
    if meter is not None:
        meter.add(stage, getattr(msg, "model", None) or model, msg.usage)
    if msg.stop_reason == "refusal":
        cat = getattr(msg.stop_details, "category", None) if msg.stop_details else None
        raise ReviewError(f"{stage}: model declined the request (category={cat})")
    if msg.stop_reason == "max_tokens":
        raise ReviewError(f"{stage}: response truncated at max_tokens")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if not text:
        raise ReviewError(f"{stage}: no text in response (stop_reason={msg.stop_reason})")
    return json.loads(text)


_str = {"type": "string"}
_str_list = {"type": "array", "items": {"type": "string"}}


def _obj(props: dict, required=None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


# ---------------------------------------------------------------------------
# Screening (Haiku)
# ---------------------------------------------------------------------------

SCREEN_SCHEMA = _obj({
    "results": {"type": "array", "items": _obj({
        "index": {"type": "integer"},
        "relevance": {"type": "number", "description": "0-10"},
        "reason": _str,
    })},
})

SCREEN_SYSTEM = """You triage newly published research papers for a reader who follows AI / computer-science research.
For every paper, rate how relevant and worth reading it is for the reader's interest keywords on a 0-10 scale:
10 = squarely on a keyword and likely significant; 7 = clearly related and worth a full read;
4 = only tangentially related; 0 = unrelated.
Penalise incremental or purely application papers slightly; reward new methods, strong empirical results and broadly useful findings.
Write `reason` as one short Korean sentence."""


def screen(papers, keywords, batch_size: int = 25, meter: CostMeter | None = None):
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
        data = _call("screen", config.SCREEN_MODEL, SCREEN_SYSTEM, prompt, SCREEN_SCHEMA,
                     effort="low", max_tokens=8000, meter=meter)
        for r in data["results"]:
            if 0 <= r["index"] < len(batch):
                rel = max(0.0, min(10.0, float(r["relevance"])))
                out[batch[r["index"]]["id"]] = (rel, r["reason"])
    return out


# ---------------------------------------------------------------------------
# 1. Read (Haiku): the full PDF -> evidence notes
# ---------------------------------------------------------------------------

NOTES_SCHEMA = _obj({
    "research_question": _str,
    "method": {**_str, "description": "How the approach works, concretely (architecture, objective, data, training/inference procedure)"},
    "claimed_contributions": _str_list,
    "experimental_setup": {**_str, "description": "Datasets/benchmarks, model sizes, compute, evaluation protocol"},
    "baselines": _str_list,
    "main_results": {"type": "array", "items": _obj({
        "claim": _str,
        "evidence": {**_str, "description": "Exact numbers as reported, with metric and comparison"},
        "location": {**_str, "description": "Table/figure/section where it appears"},
    })},
    "ablations_and_analysis": _str,
    "statistical_rigor": {**_str, "description": "Seeds, variance/error bars, significance tests, sample sizes - or their absence"},
    "reproducibility": {**_str, "description": "Code/data/weights release, hyperparameters, missing details"},
    "stated_limitations": _str_list,
    "possible_concerns": {**_str_list, "description": "Issues a careful reviewer would flag: missing baselines, unfair comparisons, contamination, cherry-picking, overclaiming"},
    "closest_prior_work": {**_str, "description": "Most related prior work the paper cites and how this differs"},
    "figures": {"type": "array", "items": _obj({"number": {"type": "integer"}, "shows": _str})},
    "headline_numbers": {"type": "array", "items": _obj({"value": _str, "meaning": _str})},
})

READER_SYSTEM = """You are a meticulous research assistant. Read the entire paper - every section, table, figure and the appendix - and write structured notes that let a senior reviewer judge it without opening the PDF.

Be factual and specific: copy numbers exactly as reported (with metric, dataset and the baseline compared against) and say where they appear. Do not evaluate or praise; record what the paper does and shows, and note plainly what is missing (no error bars, no code, missing baseline, etc.). Write in English. Keep it dense - no filler."""


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


def _meta(paper, keywords) -> str:
    return (
        f"Title: {paper['title']}\n"
        f"Authors: {', '.join(paper.get('authors', [])[:12])}\n"
        f"Venue/source: {paper.get('source_name') or paper['source_kind']}\n"
        f"Published: {paper.get('published')}\n"
        f"Reader's interest keywords: {', '.join(keywords)}\n"
    )


def read_paper(paper, pdf_path, meter):
    block, depth = _document_block(pdf_path)
    content = [block, {"type": "text", "text": f"{_meta(paper, [])}\nWrite the notes for this paper."}]
    notes = _call("read", config.READER_MODEL, READER_SYSTEM, content, NOTES_SCHEMA,
                  effort=config.READER_EFFORT, max_tokens=16000, meter=meter)
    return notes, depth


# ---------------------------------------------------------------------------
# 2. Judge (Opus): notes -> scores
# ---------------------------------------------------------------------------

JUDGE_SCHEMA = _obj({
    "methodology_score": {"type": "number", "description": "1-10"},
    "methodology_reason": {**_str, "description": "1-2 Korean sentences"},
    "novelty_score": {"type": "number", "description": "1-10"},
    "novelty_reason": {**_str, "description": "1-2 Korean sentences"},
    "strengths": {**_str_list, "description": "2-3 short Korean points"},
    "weaknesses": {**_str_list, "description": "2-3 short Korean points"},
    "verdict": {**_str, "description": "One Korean sentence: is this worth reading, and for whom"},
    "read_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
})

JUDGE_SYSTEM = """You are a senior AI/CS reviewer deciding how much a new paper is worth a busy researcher's time. You receive detailed notes taken by an assistant who read the full paper (or only the abstract when no PDF was available).

Judge only from the evidence given. Be calibrated - most papers land 5-7; reserve 8+ for clearly strong work.
- methodology_score - soundness and trustworthiness: appropriate baselines and ablations, statistical care, dataset/contamination issues, reproducibility, whether the evidence supports the claims.
  9-10 rigorous and convincing; 7-8 solid with minor gaps; 5-6 typical, some missing controls; 3-4 notable flaws; 1-2 claims unsupported.
- novelty_score - originality and contribution: new idea or insight vs. incremental tuning, size of the advance over prior work, likely influence.
  9-10 likely field-shaping; 7-8 clearly new and useful; 5-6 solid incremental; 3-4 minor variation; 1-2 nothing new.
read_confidence: high when full notes are available and consistent, low when only the abstract was read (then keep scores conservative).
Write reasons, strengths, weaknesses and verdict in Korean."""


def judge(paper, keywords, notes, depth, meter):
    evidence = (f"Abstract only (no open-access PDF):\n{paper.get('abstract')}" if depth == "abstract"
                else f"Notes from the full paper:\n{json.dumps(notes, ensure_ascii=False, indent=1)}")
    prompt = f"{_meta(paper, keywords)}\nAbstract: {paper.get('abstract')}\n\n{evidence}\n\nScore this paper."
    verdict = _call("judge", config.JUDGE_MODEL, JUDGE_SYSTEM, prompt, JUDGE_SCHEMA,
                    effort=config.JUDGE_EFFORT, max_tokens=16000, meter=meter)
    for k in ("methodology_score", "novelty_score"):
        verdict[k] = round(max(1.0, min(10.0, float(verdict[k]))), 1)
    if depth == "abstract":
        verdict["read_confidence"] = "low"
    return verdict


# ---------------------------------------------------------------------------
# 3. Write (Sonnet): notes + verdict -> Korean review and card copy
# ---------------------------------------------------------------------------

WRITE_SCHEMA = _obj({
    "title_ko": {**_str, "description": "Natural Korean translation of the title"},
    "one_liner": {**_str, "description": "One-sentence Korean takeaway (<= 60 chars)"},
    "tldr": {**_str, "description": "3-4 sentence Korean summary"},
    "problem": _str,
    "method": _str,
    "key_results": {**_str_list, "description": "3-5 concrete results with numbers"},
    "contributions": _str_list,
    "limitations": _str_list,
    "why_it_matters": _str,
    "who_should_read": _str,
    "tags": {**_str_list, "description": "3-6 short Korean or English topic tags"},
    "card": _obj({
        "category": {**_str, "description": "Short field tag, e.g. 'LLM 에이전트'"},
        "cover_headline": {**_str, "description": "Korean hook, 2 lines separated by \\n, <= 16 chars per line"},
        "cover_sub": {**_str, "description": "One Korean sentence under the headline"},
        "slides": {"type": "array", "description": "Exactly 3 slides: problem / how it works / results", "items": _obj({
            "label": {**_str, "description": "2-6 char section label, e.g. '문제', '방법', '결과'"},
            "headline": {**_str, "description": "2 lines separated by \\n, <= 16 chars per line"},
            "body": {**_str, "description": "2 short Korean sentences separated by \\n, <= 34 chars each"},
            "figure_number": {"type": "integer", "description": "Figure to show from the available list, 0 for none"},
            "highlight": {**_str, "description": "Optional big stat, e.g. '+12.4%p' or '3.2x', else empty"},
            "highlight_label": {**_str, "description": "What the stat measures, else empty"},
        })},
        "summary_rows": {"type": "array", "description": "Exactly 5 rows: 문제 / 방법 / 데이터 / 결과 / 한계", "items": _obj({
            "label": _str,
            "sub": {**_str, "description": "tiny sub-label"},
            "value": {**_str, "description": "<= 24 chars"},
            "value_sub": {**_str, "description": "<= 32 chars detail"},
        })},
        "caption": {**_str, "description": "Instagram caption in Korean, 4-8 lines, ends with 5-8 hashtags"},
    }),
})

WRITE_SYSTEM = """You write clear, accurate Korean explanations of new AI/CS papers for engineers and researchers, plus copy for a 6-slide Instagram card-news post.

Use only facts in the notes and the reviewer's verdict; keep numbers exact. Plain, natural Korean; keep technical terms in English where natural (e.g. "Transformer", "RLHF"). Limitations should reflect the reviewer's weaknesses.

Card copy is rendered large, so respect the length limits. Slides: 1 problem/motivation, 2 how the method works, 3 key results. For each slide pick the most informative figure from the available list (by number) - the method overview for slide 2, a results plot or table for slide 3. Use 0 if none fits; never invent figure numbers."""


def write(paper, notes, verdict, figures, depth, meter):
    described = {f.get("number"): f.get("shows") for f in (notes or {}).get("figures", [])}
    fig_list = "\n".join(
        f"- Figure {f['number']}: {described.get(f['number']) or f['caption'][:200]}" for f in figures
    ) or "(none available)"
    facts = (f"Abstract only:\n{paper.get('abstract')}" if depth == "abstract"
             else f"Notes from the full paper:\n{json.dumps(notes, ensure_ascii=False, indent=1)}")
    prompt = (
        f"{_meta(paper, [])}\n{facts}\n\nReviewer's verdict:\n{json.dumps(verdict, ensure_ascii=False, indent=1)}\n\n"
        f"Figures available for the cards:\n{fig_list}\n\nWrite every field."
    )
    return _call("write", config.WRITER_MODEL, WRITE_SYSTEM, prompt, WRITE_SCHEMA,
                 effort=config.WRITER_EFFORT, max_tokens=16000, meter=meter)


def review(paper: dict, keywords, pdf_path=None, figures=()):
    """Full review. Returns (review_dict, read_depth); review_dict["_cost"] has the spend."""
    meter = CostMeter()
    if pdf_path:
        notes, depth = read_paper(paper, pdf_path, meter)
    else:
        notes, depth = None, "abstract"
    verdict = judge(paper, keywords, notes, depth, meter)
    data = write(paper, notes, verdict, figures, depth, meter)

    data.update(verdict)
    data["overall_score"] = round((verdict["methodology_score"] + verdict["novelty_score"]) / 2, 1)
    valid = {f["number"] for f in figures}
    for s in data["card"]["slides"]:
        if s["figure_number"] not in valid:
            s["figure_number"] = 0
    data["_notes"] = notes
    data["_models"] = {"read": config.READER_MODEL if notes else None, "judge": config.JUDGE_MODEL,
                       "write": config.WRITER_MODEL}
    data["_cost"] = meter.summary()
    return data, depth
