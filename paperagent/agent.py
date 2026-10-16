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
import threading

import anthropic

from . import config, db, pdfs

log = logging.getLogger(__name__)

PDF_LIMIT_BYTES = 30 * 1024 * 1024  # API request limit is 32MB incl. base64 overhead
PDF_LIMIT_PAGES = 600

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # Streams send events (and pings) continuously, so a 3-minute silence means
        # the connection is stuck: fail and retry instead of waiting forever.
        _client = anthropic.Anthropic(max_retries=2, timeout=anthropic.Timeout(900, connect=20, read=180))
    return _client


# Lets the pipeline show which step is running ("원문 읽는 중", "평가 중", ...).
_listener = threading.local()


def set_call_listener(fn):
    """fn(stage, model) is called before each Claude request made by this thread."""
    _listener.fn = fn


def _notify(stage, model):
    fn = getattr(_listener, "fn", None)
    if fn:
        fn(stage, model)


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
    _notify(stage, model)
    constrained = True
    try:
        try:
            msg = _stream(model, kwargs)
        except anthropic.BadRequestError as exc:
            if not _grammar_too_large(exc):
                raise
            # The API refused to compile this schema into a grammar. Ask for
            # the same JSON in the instructions instead and check it ourselves.
            log.warning("%s: schema too large for constrained output; retrying unconstrained", stage)
            constrained = False
            kwargs["output_config"] = {"effort": effort}
            kwargs["system"] = (f"{system}\n\nReply with one JSON object only - no prose, no code fences - "
                                f"that matches this JSON Schema exactly:\n{json.dumps(schema, ensure_ascii=False)}")
            msg = _stream(model, kwargs)
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
    if constrained:
        return json.loads(text)
    return _loose_json(stage, text, schema)


def _stream(model, kwargs):
    if config.CLAUDE_FALLBACKS and _supports_fallback(model):
        # On a safety-classifier refusal the API re-runs the request on
        # Anthropic's recommended fallback model instead of failing.
        stream_ctx = client().beta.messages.stream(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    else:
        stream_ctx = client().messages.stream(**kwargs)
    with stream_ctx as stream:
        return stream.get_final_message()


def _grammar_too_large(exc) -> bool:
    text = str(getattr(exc, "message", "") or exc).lower()
    return "grammar" in text and ("too large" in text or "too complex" in text or "simplify" in text)


def _loose_json(stage, text, schema) -> dict:
    """Parse JSON written without a schema constraint and check the top-level fields."""
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1]) if start >= 0 < end else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        raise ReviewError(f"{stage}: response was not a JSON object")
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise ReviewError(f"{stage}: response is missing {', '.join(missing)}")
    return data


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
    "background": {**_str, "description": "Why the problem matters and what existing approaches do / where they fall short"},
    "key_idea": {**_str, "description": "The core insight that makes the approach work, in 2-3 sentences"},
    "method": {**_str, "description": "How the approach works, concretely (architecture, objective, data, training/inference procedure)"},
    "method_steps": {"type": "array", "description": "The method as ordered steps, each with concrete details (inputs, outputs, losses, key hyperparameters)",
                     "items": _obj({"step": _str, "detail": _str})},
    "formulation": {**_str, "description": "Key objective, loss or algorithm written out (plain text or LaTeX); empty if none"},
    "claimed_contributions": _str_list,
    "datasets": {**_str_list, "description": "Every dataset/benchmark used, with split or size when given"},
    "models": {**_str_list, "description": "Models evaluated (names, sizes) and what they are used for"},
    "metrics": {**_str_list, "description": "Evaluation metrics and what higher/lower means"},
    "experimental_setup": {**_str, "description": "Evaluation protocol, compute, training budget, important settings"},
    "baselines": _str_list,
    "main_results": {"type": "array", "description": "Every important number from the main tables and figures", "items": _obj({
        "claim": _str,
        "evidence": {**_str, "description": "Exact numbers as reported, with metric and comparison"},
        "location": {**_str, "description": "Table/figure/section where it appears"},
    })},
    "ablations_and_analysis": {**_str_list, "description": "Each ablation or analysis finding, with numbers"},
    "statistical_rigor": {**_str, "description": "Seeds, variance/error bars, significance tests, sample sizes - or their absence"},
    "reproducibility": {**_str, "description": "Code/data/weights release, hyperparameters, missing details"},
    "stated_limitations": _str_list,
    "possible_concerns": {**_str_list, "description": "Issues a careful reviewer would flag: missing baselines, unfair comparisons, contamination, cherry-picking, overclaiming"},
    "closest_prior_work": {**_str, "description": "Most related prior work the paper cites and how this differs"},
    "terminology": {"type": "array", "description": "Technical terms a reader must know, defined as the paper uses them",
                    "items": _obj({"term": _str, "definition": _str})},
    "figures": {"type": "array", "items": _obj({"number": {"type": "integer"}, "shows": _str})},
    "headline_numbers": {"type": "array", "items": _obj({"value": _str, "meaning": _str})},
})

READER_SYSTEM = """You are a meticulous research assistant. Read the entire paper - every section, table, figure and the appendix - and write structured notes complete enough that someone can explain the paper in full, and a senior reviewer can judge it, without opening the PDF.

Be exhaustive and specific:
- Method: write it as ordered steps a reader could re-implement, with the concrete inputs, outputs, objectives and key hyperparameters.
- Results: record every important number from the main tables and figures, copied exactly as reported (metric, dataset, the baseline it is compared against) and where it appears.
- Setup: list every dataset, model and metric used.
- Note plainly what is missing (no error bars, no code, missing baseline, etc.).
Do not evaluate or praise; record what the paper does and shows. Never guess a number that is not in the paper. Write in English."""


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
                  effort=config.READER_EFFORT, max_tokens=32000, meter=meter)
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

JUDGE_SYSTEM = """You are a university professor in AI/CS giving a fair, constructive assessment of a new paper - the kind of feedback a supportive advisor gives on a solid submission. You receive detailed notes taken by an assistant who read the full paper (or only the abstract when no PDF was available).

Judge only from the evidence given. Credit what the paper does well; a competent paper with clear contributions should score well even if it has the usual gaps. Typical published work lands around 6.5-8; use 9+ for excellent work; go below 5 only for serious, specific problems.
- methodology_score - soundness and trustworthiness: appropriate baselines and ablations, statistical care, dataset/contamination issues, reproducibility, whether the evidence supports the claims.
  9-10 rigorous and convincing; 7.5-8.5 solid, minor gaps; 6-7 reasonable with some missing controls; 4-5 notable gaps that weaken the claims; 1-3 claims largely unsupported.
- novelty_score - originality and contribution: new idea or insight vs. incremental tuning, size of the advance over prior work, likely usefulness to others.
  9-10 likely to change how people work on this; 7.5-8.5 clearly new and useful; 6-7 a meaningful extension of known ideas; 4-5 a small variation; 1-3 nothing new.
read_confidence: high when full notes are available and consistent, low when only the abstract was read (then stay moderate rather than harsh).
Tone: write reasons, strengths, weaknesses and verdict in polite, constructive Korean. Phrase weaknesses as suggestions, e.g. "~에 대한 보완이 필요해 보입니다", "~는 다소 미흡할 수 있습니다", "~에 대한 추가 연구가 필요할 수 있습니다" - never blunt verdicts like "~가 없다", "~가 부족하다"."""


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

def _rows(desc, **fields) -> dict:
    return {"type": "array", "description": desc, "items": _obj(fields)}


WRITE_SCHEMA = _obj({
    "title_ko": {**_str, "description": "Natural Korean translation of the title"},
    "one_liner": {**_str, "description": "One-sentence Korean takeaway with the key number if there is one (<= 80 chars)"},
    "tldr": {**_str, "description": "Executive summary, 6-9 Korean sentences in one paragraph: the problem, why existing approaches fall short, "
                                    "the core idea, how it works, the main results with numbers, the main limitation, and who benefits. "
                                    "Someone who reads only this must understand the whole paper."},
    "key_points": {**_str_list, "description": "4-6 self-contained Korean bullet points, each a concrete fact (with numbers where possible)"},
    "background": {**_str, "description": "3-5 Korean sentences: context, why it matters, what prior approaches do and where they fall short"},
    "problem": {**_str, "description": "2-4 Korean sentences: the exact problem the paper tackles"},
    "key_idea": {**_str, "description": "2-3 Korean sentences: the core insight in plain words"},
    "method": {**_str, "description": "4-6 Korean sentences: overview of how the method works"},
    "method_steps": _rows("3-6 ordered steps of the method", title={**_str, "description": "Short Korean step name"},
                          detail={**_str, "description": "1-3 Korean sentences with the concrete details"}),
    "setup": _rows("4-6 rows: 데이터셋 / 모델 / 비교 대상 / 평가 지표 / 학습·연산 (skip a row only if the paper has nothing on it)",
                   label=_str, value={**_str, "description": "Concrete names, sizes and settings"}),
    "results": _rows("4-8 most important results, most important first",
                     item={**_str, "description": "What was measured (benchmark/metric), Korean"},
                     value={**_str, "description": "The paper's number, exactly as reported"},
                     comparison={**_str, "description": "Baseline or previous best and its number, or the change; empty if none"},
                     source={**_str, "description": "Where it is reported, e.g. 'Table 2', 'Fig. 3'; empty if unknown"}),
    "analysis": {**_str_list, "description": "2-5 Korean findings from ablations/analysis, with numbers"},
    "contributions": _str_list,
    "limitations": {**_str_list, "description": "3-5 constructive Korean points (what could be improved or studied further)"},
    "prior_work": {**_str, "description": "2-3 Korean sentences: closest prior work and how this paper differs"},
    "why_it_matters": {**_str, "description": "2-3 Korean sentences"},
    "who_should_read": _str,
    "glossary": _rows("3-6 terms a reader needs, explained simply in Korean", term=_str, meaning=_str),
    "tags": {**_str_list, "description": "3-6 short Korean or English topic tags"},
})

WRITE_SYSTEM = """You write clear, accurate and thorough Korean explanations of new AI/CS papers for engineers and researchers.

Goal: a reader who only reads your summary should understand the whole paper - what problem, why it is hard, the idea, how the method works step by step, how it was tested, what the numbers are, and what the limits are. Be specific and concrete; avoid vague phrases like "성능이 크게 향상" without the number.

Accuracy rules:
- Use only facts in the notes and the reviewer's verdict. Never invent or round numbers; copy them exactly with their metric, and give the source (Table/Figure) when the notes have it.
- If the notes do not cover something, say so ("논문에 보고되지 않음") instead of guessing.
- Limitations must reflect the paper's stated limitations and the reviewer's weaknesses, written constructively as points to improve or study further ("~에 대한 보완이 필요해 보입니다", "~는 다소 미흡할 수 있습니다", "~에 대한 추가 연구가 필요할 수 있습니다"), not as blunt verdicts.

Style: plain, natural Korean; keep technical terms in English where natural (e.g. "Transformer", "RLHF") and explain them in the glossary."""

CARD_SCHEMA = _obj({
    "category": {**_str, "description": "Short field tag, e.g. 'LLM 에이전트'"},
    "cover_headline": {**_str, "description": "A question a curious non-expert would ask, answered by this paper, ending with '?', 2 lines separated by \\n, <= 16 chars per line - e.g. '작은 AI도 스스로\\n검산하면 더 똑똑해질까?'. No clickbait"},
    "cover_sub": {**_str, "description": "One Korean sentence that hints at the answer, with the key number if there is one (<= 70 chars)"},
    "cover_points": {**_str_list, "description": "Exactly 3 Korean takeaways, each <= 44 chars, at least one with a number"},
    "problem": _obj({
        "headline": {**_str, "description": "2 lines separated by \\n, <= 16 chars per line"},
        "points": {**_str_list, "description": "3 Korean sentences, each <= 75 chars: the situation, what goes wrong, why it matters"},
        "context": {**_str, "description": "2-3 plain Korean sentences of background, <= 170 chars (shown when there is no figure)"},
        "figure_number": {"type": "integer", "description": "Figure to show, 0 for none"},
    }),
    "idea": _obj({
        "headline": {**_str, "description": "2 lines separated by \\n, <= 16 chars per line"},
        "body": {**_str, "description": "2-3 Korean sentences, <= 170 chars in total: the core idea"},
        "figure_number": {"type": "integer", "description": "The method overview figure, 0 for none"},
    }),
    "steps": _rows("3-4 method steps for the card",
                   title={**_str, "description": "<= 16 chars"}, detail={**_str, "description": "<= 85 chars"}),
    "setup": _rows("4 rows: 데이터셋 / 모델 / 비교 대상 / 평가 지표",
                   label={**_str, "description": "<= 8 chars"}, value={**_str, "description": "<= 70 chars"}),
    "findings": {**_str_list, "description": "2 plain Korean sentences on what the extra experiments showed, each <= 80 chars"},
    "results": _obj({
        "headline": {**_str, "description": "2 lines separated by \\n, <= 16 chars per line"},
        "highlight": {**_str, "description": "The single most important number, e.g. '+12.4%p'"},
        "highlight_label": {**_str, "description": "What it measures, <= 30 chars"},
        "rows": _rows("3-4 results", item={**_str, "description": "<= 22 chars"}, value={**_str, "description": "<= 16 chars"},
                      note={**_str, "description": "comparison or context in plain words, <= 40 chars"}),
        "bars": _rows("2-3 bars comparing the SAME metric (this paper vs baselines), drawn to scale; empty when the paper has no such comparison",
                      label={**_str, "description": "<= 16 chars, e.g. '이 논문', '기존 최고 방법'"},
                      value={"type": "number", "description": "The plain number exactly as reported (e.g. 53.6)"},
                      display={**_str, "description": "How to show it, e.g. '53.6%'"},
                      highlight={"type": "boolean", "description": "true only for this paper's result"}),
        "figure_number": {"type": "integer", "description": "A results plot or table, 0 for none"},
    }),
    "so_what": _obj({
        "headline": {**_str, "description": "The practical meaning in one phrase, <= 26 Korean chars"},
        "items": _rows("Exactly 3 honest answers to '그래서 나랑 무슨 상관?' for an ordinary reader: what could change in products, "
                       "work or daily life, what it costs or when it might arrive. No hype; if the paper is far from everyday "
                       "life, say what it would change for whom.",
                       title={**_str, "description": "<= 18 chars"}, detail={**_str, "description": "<= 75 chars"}),
    }),
    "verdict": _obj({
        "strengths": {**_str_list, "description": "2 Korean points, each <= 60 chars"},
        "weaknesses": {**_str_list, "description": "2 constructive Korean points (what could be improved or studied further), each <= 60 chars"},
        "who": {**_str, "description": "Who should read it, <= 60 chars"},
    }),
    "summary_rows": _rows("Exactly 5 rows: 문제 / 방법 / 데이터 / 결과 / 한계",
                          label=_str, sub={**_str, "description": "tiny sub-label"},
                          value={**_str, "description": "<= 24 chars"}, value_sub={**_str, "description": "<= 40 chars detail"}),
    "terms": _rows("0-4 hard words used in the card copy, each marked with * right after the word where it appears (e.g. 'MATH*')",
                   term={**_str, "description": "The word exactly as written before the *"},
                   meaning={**_str, "description": "Plain Korean explanation, <= 60 chars, no analogies"}),
    "caption": {**_str, "description": "Instagram caption in Korean for a general audience, 8-14 lines: hook, the problem, the idea, "
                                      "2-3 results with numbers, what could be improved, who it helps. No hashtags."},
    "hashtags": {**_str_list, "description": "2 hashtags (with #) for this paper's field that people actually search on Instagram, "
                                            "e.g. #LLM, #컴퓨터비전, #생성형AI, #자율주행 - only if the paper is really about it"},
})

CARD_SYSTEM = """You turn a Korean paper review into copy for an Instagram card-news post (question cover, problem, key idea, how it works, setup, results, "so what for me", verdict, summary; a fixed closing slide follows).

Audience: people with no AI or engineering background. Write so they can follow every card:
- Explain things as they actually are, in plain words and enough detail - do not explain by analogy or metaphor (no "마치 ~처럼", no comparing the method to everyday objects).
- When a technical word is unavoidable (a benchmark, method or metric name), either explain it in the same sentence or put * right after it (e.g. "MATH*") and define it in `terms`. Use at most 4 terms.
- Say what numbers mean ("정답률 41.2% → 53.6%, 100문제 중 약 12문제를 더 맞힘") rather than leaving bare figures, but never change or invent a number.
- Short, clear sentences; natural Korean; no hype.
- The cover is a question the reader wants answered; the slides answer it.
- Weaknesses and limits are constructive: "~에 대한 보완이 필요해 보입니다", "~는 다소 미흡할 수 있습니다", "~에 대한 추가 연구가 필요할 수 있습니다".

Use only facts from the review. Card copy is rendered large, so respect every length limit - shorten wording, never cut a number.

For each card figure pick the most informative figure from the available list (by number) - the method overview for `idea`, a results plot or table for `results`, a motivating example for `problem`. Use 0 if none fits; never invent figure numbers or reuse one figure twice."""


def write(paper, notes, verdict, figures, depth, meter):
    """Korean review (WRITE_SCHEMA), then the card copy from that review (CARD_SCHEMA).
    Two smaller schemas instead of one keeps each structured-output grammar small."""
    described = {f.get("number"): f.get("shows") for f in (notes or {}).get("figures", [])}
    fig_list = "\n".join(
        f"- Figure {f['number']}: {described.get(f['number']) or f['caption'][:200]}" for f in figures
    ) or "(none available)"
    facts = (f"Abstract only:\n{paper.get('abstract')}" if depth == "abstract"
             else f"Notes from the full paper:\n{json.dumps(notes, ensure_ascii=False, indent=1)}")
    verdict_json = json.dumps(verdict, ensure_ascii=False, indent=1)
    prompt = f"{_meta(paper, [])}\n{facts}\n\nReviewer's verdict:\n{verdict_json}\n\nWrite every field."
    data = _call("write", config.WRITER_MODEL, WRITE_SYSTEM, prompt, WRITE_SCHEMA,
                 effort=config.WRITER_EFFORT, max_tokens=32000, meter=meter)
    card_prompt = (
        f"{_meta(paper, [])}\nReview:\n{json.dumps(data, ensure_ascii=False, indent=1)}\n\n"
        f"Reviewer's verdict:\n{verdict_json}\n\nFigures available for the cards:\n{fig_list}\n\nWrite every field."
    )
    data["card"] = _call("card", config.WRITER_MODEL, CARD_SYSTEM, card_prompt, CARD_SCHEMA,
                         effort=config.WRITER_EFFORT, max_tokens=16000, meter=meter)
    return data


def review(paper: dict, keywords, pdf_path=None, figures=()):
    """Full review. Returns (review_dict, read_depth); review_dict["_cost"] has the spend."""
    meter = CostMeter()
    try:
        if pdf_path:
            notes, depth = read_paper(paper, pdf_path, meter)
        else:
            notes, depth = None, "abstract"
        verdict = judge(paper, keywords, notes, depth, meter)
        data = write(paper, notes, verdict, figures, depth, meter)
    except Exception as exc:
        exc.cost_usd = meter.total_usd  # calls that already ran are still billed
        raise

    data.update(verdict)
    data["overall_score"] = round((verdict["methodology_score"] + verdict["novelty_score"]) / 2, 1)
    valid = {f["number"] for f in figures}
    used = set()
    for key in ("problem", "idea", "results"):
        slide = data["card"].get(key) or {}
        n = slide.get("figure_number") or 0
        slide["figure_number"] = n if n in valid and n not in used else 0
        used.add(slide["figure_number"])
    data["_notes"] = notes
    data["_models"] = {"read": config.READER_MODEL if notes else None, "judge": config.JUDGE_MODEL,
                       "write": config.WRITER_MODEL}
    data["_cost"] = meter.summary()
    return data, depth


# ---------------------------------------------------------------------------
# TECH card news: trending projects and tech news
# ---------------------------------------------------------------------------

TECH_SCREEN_SCHEMA = _obj({
    "results": {"type": "array", "items": _obj({
        "index": {"type": "integer"},
        "interest": {"type": "number", "description": "0-10"},
        "reason": _str,
    })},
})

TECH_SCREEN_SYSTEM = """You pick items for a Korean Instagram account that explains new technology to curious people - students, office workers and developers - with a focus on AI and software.
Rate each item 0-10 for how good a post it would make:
10 = a genuinely new tool, model, research result or event many people would want to know about or try, from a credible source;
7 = clearly interesting and explainable in a few cards; 4 = niche, incremental or hard to make interesting; 0 = not suitable.
Score 0-2 for: politics, crime, celebrity or company gossip, crypto/financial speculation, job posts, plain self-promotion,
paywalled or opinion pieces without new facts, and anything adult or harmful. Prefer things with something to see or try.
Write `reason` as one short Korean sentence."""


def screen_tech(items, batch_size: int = 30, meter: CostMeter | None = None):
    """Returns {item_id: (interest, reason)}."""
    out = {}
    for i in range(0, len(items), batch_size):
        batch = items[i: i + batch_size]
        listing = "\n\n".join(
            f"[{j}] ({it['source']}) {it['title']}\nNumbers: {json.dumps(it.get('meta') or {}, ensure_ascii=False)}\n"
            f"About: {(it.get('summary') or '')[:600]}"
            for j, it in enumerate(batch)
        )
        data = _call("screen", config.SCREEN_MODEL, TECH_SCREEN_SYSTEM,
                     f"Rate each of the {len(batch)} items below (use the bracketed index).\n\n{listing}",
                     TECH_SCREEN_SCHEMA, effort="low", max_tokens=8000, meter=meter)
        for r in data["results"]:
            if 0 <= r["index"] < len(batch):
                out[batch[r["index"]]["id"]] = (max(0.0, min(10.0, float(r["interest"]))), r["reason"])
    return out


_L = lambda n: {**_str, "description": f"<= {n} Korean chars"}  # noqa: E731
_H = {**_str, "description": "2 lines separated by \\n, <= 16 chars per line"}

TECH_SCHEMA = _obj({
    "title_ko": {**_str, "description": "What it is, as a natural Korean title (<= 40 chars)"},
    "one_liner": {**_str, "description": "One Korean sentence: what it is and why people are paying attention (<= 90 chars)"},
    "summary": {**_str, "description": "5-7 Korean sentences: what it is, who made it, what it does, how it works, why it is trending now, "
                                      "what to be careful about. Only facts from the material."},
    "category": {**_str, "description": "Short field tag, e.g. 'AI 에이전트', '개발 도구', '오픈소스 모델', '보안'"},
    "kind": {"type": "string", "enum": ["project", "model", "research", "news", "product"]},
    "cover_headline": {**_str, "description": "A question a curious non-expert would ask, ending with '?', 2 lines separated by \\n, "
                                             "<= 16 chars per line, no clickbait, e.g. '코드 리뷰를\\nAI한테 맡겨도 될까?'"},
    "cover_sub": _L(70),
    "what": _obj({"headline": _H,
                  "points": {**_str_list, "description": "Exactly 3 Korean sentences, each <= 75 chars: what it is, what it does, what is new about it"},
                  "why_now": {**_str, "description": "2 Korean sentences on why people are talking about it now, with the numbers given (<= 160 chars)"}}),
    "maker": {**_str, "description": "The company or organisation that made it, as the material names it (e.g. 'Meta', 'Apple', "
                                    "'Google DeepMind', 'NVIDIA'); empty if it is not clearly stated or an individual made it"},
    "demo": _obj({"headline": _H, "body": {**_str, "description": "1-2 Korean sentences describing what the picture/clip shows or what using it looks like (<= 110 chars)"},
                  "use_clip": {"type": "boolean", "description": "true only if a demo clip is available and the text next to it shows it "
                                                                  "demonstrates what this post and this slide describe; false otherwise"}}),
    "how": _rows("3-4 steps: how it works or how it is used", title=_L(16), detail=_L(85)),
    "so_what": _obj({
        "headline": _H,
        "items": _rows("Exactly 3 honest answers to '그래서 나랑 무슨 상관?': who benefits and how, what changes, what it costs or risks. No hype.",
                       title=_L(18), detail=_L(75)),
    }),
    "try_it": _obj({
        "headline": _H,
        "steps": _rows("1-3 steps to try it. Use a command only if it appears in the material, copied exactly; otherwise describe the step and leave code empty",
                       title=_L(18), detail=_L(70), code={**_str, "description": "One short shell command from the material, or empty"}),
    }),
    "caveats": {**_str_list, "description": "2-3 constructive Korean points to keep in mind (maturity, licence, cost, accuracy, privacy)"},
    "summary_rows": _rows("Exactly 5 rows: 무엇 / 특징 / 화제 / 활용 / 주의", label=_str, value=_L(24), value_sub=_L(40)),
    "terms": _rows("0-4 hard words used in the card copy, each marked with * right after the word where it appears",
                   term={**_str, "description": "The word exactly as written before the *"},
                   meaning={**_str, "description": "Plain Korean explanation, <= 60 chars, no analogies"}),
    "caption": {**_str, "description": "Instagram caption in Korean, 8-12 lines: hook question, what it is, 2-3 facts with numbers, "
                                      "who it helps, one thing to keep in mind, and a question inviting comments. No hashtags."},
    "hashtags": {**_str_list, "description": "2 hashtags (with #) people actually search, matching the topic, e.g. #개발자, #오픈소스, #생성형AI"},
})

TECH_SYSTEM = """You turn a trending project or tech news item into a Korean Instagram card-news post and a short explainer for the dashboard.

Audience: people with no engineering background who are curious about technology, plus developers.
- Explain things as they actually are, in plain words - no analogies or metaphors ("마치 ~처럼").
- Mark unavoidable technical words with * right after the word (e.g. "API*") and define them in `terms` (at most 4).
- Use only facts in the material and the numbers given. Never invent features, benchmarks, prices, dates or commands. If something is unknown, leave it out.
- Say what numbers mean (e.g. "하루 만에 GitHub 스타 2,300개 - 그만큼 많은 개발자가 관심을 표시했다는 뜻").
- Caveats are constructive ("~는 아직 초기 단계라 보완이 필요해 보입니다").
- Short, clear sentences; natural Korean; no hype. Respect every length limit."""


def write_tech(item: dict, context: dict, meter: CostMeter | None = None) -> dict:
    """Korean explainer and card copy for one tech item (TECH_SCHEMA)."""
    from .collectors import tech as tech_src

    source = tech_src.SOURCES.get(item["source"], {}).get("name", item["source"])
    clip = context.get("clip")
    media = (f"a demo clip (the text next to it in the source: \"{clip.get('near') or 'none'}\")" if clip
             else "pictures" if context.get("images") else "nothing to show")
    prompt = (
        f"Source: {source}\nTitle: {item['title']}\nLink: {item.get('url')}\n"
        f"Numbers from the source: {json.dumps(item.get('meta') or {}, ensure_ascii=False)}\n"
        f"Listing text: {item.get('summary') or ''}\n"
        f"Available to show: {media}.\n\n"
        f"Material (README / article / model card, may be cut off):\n{context.get('text') or '(none)'}\n\nWrite every field."
    )
    data = _call("write", config.WRITER_MODEL, TECH_SYSTEM, prompt, TECH_SCHEMA,
                 effort=config.WRITER_EFFORT, max_tokens=16000, meter=meter)
    data["_models"] = {"screen": config.SCREEN_MODEL, "write": config.WRITER_MODEL}
    return data
