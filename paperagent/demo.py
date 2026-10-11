"""Demo data for trying the dashboard without an API key or network access.

    python -m paperagent demo          add a fictional reviewed paper (+ a few queued ones)
    python -m paperagent demo --clear  remove all demo data

The paper, its PDF and its review are made up for illustration; every demo
row has a uid starting with "demo:" and a title ending in "(demo)".
"""
import copy
import tempfile
from pathlib import Path

import pymupdf
from sqlalchemy import delete, select

from . import cardnews, db, pdfs, storage

DEMO_UID = "demo:self-verification"

SAMPLE_PAPER = {
    "uid": DEMO_UID,
    "source_kind": "arxiv",
    "source_name": "arXiv · cs.CL",
    "title": "Self-Verification Loops Make Small Language Models Better Reasoners (demo)",
    "authors": ["Ana Kim", "Ben Lee", "Chris Park"],
    "abstract": (
        "Small language models often commit to a wrong intermediate step and never recover. We propose a "
        "self-verification loop in which the same model generates an answer, checks it step by step, and revises "
        "it when the check fails. Without any additional training, the loop improves a 7B model by 12.4 points on "
        "MATH and gives consistent gains on three other reasoning benchmarks, at about three times the inference cost."
    ),
    "published": "2026-10-09",
    "url": "https://arxiv.org/abs/2610.01234",
    "pdf_url": "https://arxiv.org/pdf/2610.01234",
    "categories": ["cs.CL", "cs.AI"],
    "matched_keywords": ["large language model", "AI agent"],
}

SAMPLE_REVIEW = {
    "title_ko": "작은 언어모델도 스스로 검증하며 추론을 개선한다",
    "one_liner": "답을 낸 뒤 스스로 검증하는 루프만으로 7B 모델의 수학 추론이 크게 오른다",
    "tldr": (
        "작은 언어모델이 중간 단계에서 한 번 틀리면 끝까지 틀리는 문제를 다룹니다. 같은 모델이 답을 만들고, "
        "단계별로 검증하고, 틀리면 고치는 루프를 추가 학습 없이 붙였습니다. 7B 모델의 MATH 정답률이 12.4%p 올랐고 "
        "다른 추론 벤치마크 세 곳에서도 꾸준히 개선됐습니다. 대신 추론 비용은 약 3배가 듭니다."
    ),
    "problem": "작은 모델은 추론 도중 한 단계에서 실수하면 그 오류를 바로잡지 못하고 틀린 답으로 끝나는 경우가 많습니다.",
    "method": (
        "생성 → 검증 → 수정의 세 단계를 반복합니다. 검증 단계에서는 같은 모델이 풀이를 한 줄씩 다시 확인하고, "
        "틀린 단계를 찾으면 그 지점부터 다시 풀게 합니다. 별도 검증 모델이나 추가 학습은 쓰지 않습니다."
    ),
    "key_results": [
        "MATH 정답률 41.2% → 53.6% (+12.4%p, 7B 모델)",
        "GSM8K, ARC-Challenge, StrategyQA에서도 2~6%p 개선",
        "검증 4회까지는 정답률이 오르고 그 이후로는 거의 변화 없음",
    ],
    "contributions": [
        "추가 학습 없이 붙일 수 있는 자기 검증 루프를 제안",
        "검증 횟수와 정답률·비용의 관계를 체계적으로 분석",
    ],
    "limitations": [
        "추론 비용이 약 3배로 늘어남",
        "코드 생성이나 일반 대화 과제에서는 검증하지 않음",
        "시드별 분산이 보고되지 않음",
    ],
    "why_it_matters": "큰 모델을 쓰기 어려운 환경에서 작은 모델의 추론 품질을 학습 없이 끌어올릴 수 있는 실용적인 방법입니다.",
    "who_should_read": "온디바이스나 저비용 환경에서 LLM 추론 품질을 높이려는 엔지니어와 연구자",
    "tags": ["LLM", "Reasoning", "Self-verification", "Inference"],
    "methodology_score": 7.5,
    "methodology_reason": "베이스라인과 벤치마크 구성은 충실하지만 시드별 분산과 비용 대비 비교가 부족합니다.",
    "novelty_score": 8.0,
    "novelty_reason": "자기 검증 아이디어 자체는 알려져 있지만, 학습 없이 작은 모델에서 일관된 이득을 보인 점이 새롭습니다.",
    "strengths": ["추가 학습 없이 바로 적용 가능", "네 개 벤치마크에서 일관된 개선"],
    "weaknesses": ["추론 비용 3배", "분산 보고 없음"],
    "verdict": "작은 모델로 추론 품질을 끌어올려야 하는 팀이라면 읽어 볼 만합니다.",
    "read_confidence": "high",
    "overall_score": 7.8,
    "card": {
        "category": "LLM 추론",
        "cover_headline": "작은 모델이\n스스로 답을 검증합니다",
        "cover_sub": "추가 학습 없이 검증 루프만 붙여 수학 정답률을 끌어올렸습니다.",
        "slides": [
            {"label": "문제", "headline": "작은 모델은 왜\n추론에 약할까요?",
             "body": "한 단계에서 틀리면 끝까지 틀립니다.\n스스로 오류를 고칠 장치가 없습니다.",
             "figure_number": 0, "highlight": "7B", "highlight_label": "실험에 쓴 모델 크기"},
            {"label": "방법", "headline": "답을 내고\n다시 검증합니다",
             "body": "생성 → 검증 → 수정 루프를 반복합니다.\n검증도 같은 모델이 맡습니다.",
             "figure_number": 1, "highlight": "", "highlight_label": ""},
            {"label": "결과", "headline": "정답률이\n크게 올랐습니다",
             "body": "네 가지 벤치마크 모두 개선됐습니다.\n검증 4회 이후로는 거의 그대로입니다.",
             "figure_number": 2, "highlight": "+12.4%p", "highlight_label": "MATH 정답률 향상"},
        ],
        "summary_rows": [
            {"label": "문제", "sub": "배경", "value": "작은 모델의 추론 오류", "value_sub": "한 번 틀리면 복구 못 함"},
            {"label": "방법", "sub": "핵심", "value": "생성·검증·수정 루프", "value_sub": "추가 학습 없음"},
            {"label": "데이터", "sub": "평가", "value": "추론 벤치마크 4종", "value_sub": "MATH · GSM8K 등"},
            {"label": "결과", "sub": "성능", "value": "MATH +12.4%p", "value_sub": "검증 4회까지 상승"},
            {"label": "한계", "sub": "알아 둘 점", "value": "추론 비용 약 3배", "value_sub": "분산 보고 없음"},
        ],
        "caption": (
            "작은 언어모델도 스스로 답을 검증하면 훨씬 잘 풉니다.\n"
            "추가 학습 없이 '생성 → 검증 → 수정' 루프만 붙여 7B 모델의 MATH 정답률을 12.4%p 올렸어요.\n"
            "대신 추론 비용은 약 3배입니다.\n\n"
            "#AI #LLM #논문리뷰 #추론 #머신러닝 #STARWARS"
        ),
    },
}

QUEUED = [
    ("demo:queued-1", "Sparse Mixture-of-Experts Routing Without Load Balancing Loss (demo)", "screened", 8.5,
     "MoE 라우팅을 새로 제안해 관심 주제와 직접 관련됨"),
    ("demo:queued-2", "A Benchmark for Multimodal Agents in Spreadsheets (demo)", "screened", 7.5,
     "멀티모달 에이전트 평가 벤치마크"),
    ("demo:skipped-1", "Protein Folding with Graph Transformers (demo)", "skipped", 2.0,
     "관심 키워드와 거리가 먼 생물학 응용 논문"),
]


# --------------------------------------------------------------------------
# A small two-figure PDF drawn with PyMuPDF (no extra dependencies)
# --------------------------------------------------------------------------

_BODY = (
    "Small language models often commit to an incorrect intermediate step and never recover. "
    "We study a simple self-verification loop that requires no additional training. "
)


def _paragraph(page, top, lines=6):
    page.insert_textbox(pymupdf.Rect(60, top, 552, top + 12 * lines + 4), (_BODY * 4)[: 95 * lines], fontsize=9)


def make_pdf(path):
    doc = pymupdf.open()

    # Page 1: method diagram (Figure 1)
    page = doc.new_page(width=612, height=792)
    page.insert_text((60, 60), "Self-Verification Loops Make Small Language Models Better Reasoners", fontsize=13)
    _paragraph(page, 80)
    labels = ["Question", "Generate", "Verify", "Revise"]
    shape = page.new_shape()
    for i, label in enumerate(labels):
        x = 80 + i * 120
        shape.draw_rect(pymupdf.Rect(x, 210, x + 92, 262))
        shape.finish(color=(0.2, 0.31, 0.48), fill=(0.93, 0.95, 0.98), width=1)
        if i:
            shape.draw_line((x - 26, 236), (x - 4, 236))
            shape.finish(color=(0.2, 0.31, 0.48), width=1, closePath=False)
            shape.draw_polyline([(x - 9, 232), (x - 4, 236), (x - 9, 240)])
            shape.finish(color=(0.2, 0.31, 0.48), width=1, closePath=False)
    shape.draw_bezier((486, 266), (440, 320), (260, 320), (226, 266))
    shape.finish(color=(0.7, 0.25, 0.25), width=1, dashes="[3] 0", closePath=False)
    shape.commit()
    for i, label in enumerate(labels):
        page.insert_textbox(pymupdf.Rect(80 + i * 120, 228, 172 + i * 120, 250), label, fontsize=10, align=1)
    page.insert_textbox(pymupdf.Rect(300, 318, 420, 332), "reject → retry", fontsize=8, color=(0.7, 0.25, 0.25), align=1)
    page.insert_textbox(pymupdf.Rect(60, 348, 552, 378), "Figure 1: Overview of the self-verification loop.", fontsize=9)
    _paragraph(page, 390, lines=12)

    # Page 2: results chart (Figure 2)
    page = doc.new_page(width=612, height=792)
    x0, y0, x1, y1 = 140, 80, 480, 280
    shape = page.new_shape()
    for k in range(5):  # grid
        y = y1 - k * (y1 - y0) / 4
        shape.draw_line((x0, y), (x1, y))
        shape.finish(color=(0.88, 0.88, 0.88), width=0.5, closePath=False)
    shape.draw_polyline([(x0, y0), (x0, y1), (x1, y1)])  # axes
    shape.finish(color=(0.2, 0.2, 0.2), width=1, closePath=False)
    series = [([41.2, 47.8, 51.3, 53.1, 53.6], (0.12, 0.47, 0.71)), ([41.2, 42.0, 42.3, 42.4, 42.4], (1.0, 0.5, 0.05))]
    for values, color in series:
        pts = [(x0 + 20 + i * 75, y1 - (v - 40) / 16 * (y1 - y0)) for i, v in enumerate(values)]
        shape.draw_polyline(pts)
        shape.finish(color=color, width=1.6, closePath=False)
        for p in pts:
            shape.draw_circle(p, 2.6)
            shape.finish(color=color, fill=color)
    shape.commit()
    for k in range(5):
        page.insert_text((x0 - 22, y1 - k * (y1 - y0) / 4 + 3), f"{40 + k * 4}", fontsize=7)
    for i in range(5):
        page.insert_text((x0 + 18 + i * 75, y1 + 12), str(i), fontsize=7)
    page.insert_text((x0 + 120, y1 + 26), "Verification rounds", fontsize=8)
    page.insert_text((x0 + 20, y0 + 14), "Self-Verify (ours)", fontsize=8, color=(0.12, 0.47, 0.71))
    page.insert_text((x0 + 20, y0 + 26), "Self-Consistency", fontsize=8, color=(1.0, 0.5, 0.05))
    page.insert_textbox(pymupdf.Rect(60, 318, 552, 346), "Figure 2: Accuracy on MATH as verification rounds increase.", fontsize=9)
    _paragraph(page, 360, lines=14)

    doc.save(str(path))
    return Path(path)


# --------------------------------------------------------------------------

def clear():
    with db.engine().begin() as conn:
        ids = [r[0] for r in conn.execute(select(db.papers.c.id).where(db.papers.c.uid.like("demo:%")))]
        conn.execute(delete(db.papers).where(db.papers.c.uid.like("demo:%")))
    for pid in ids:
        storage.delete_prefix(f"figures/{pid}/")
        storage.delete_prefix(f"cards/{pid}/")
    return len(ids)


def seed(render_cards=True):
    """Insert the demo paper (reviewed, with cards) and a few queued ones."""
    clear()
    db.insert_paper(SAMPLE_PAPER)
    pid = db.get_paper_by_uid(DEMO_UID)["id"]
    review = copy.deepcopy(SAMPLE_REVIEW)
    with tempfile.TemporaryDirectory(prefix="demo-") as tmp:
        tmp = Path(tmp)
        figures = pdfs.extract_figures(make_pdf(tmp / "demo.pdf"), tmp / "figs")
        for f in figures:
            f["key"] = storage.put_file(f"figures/{pid}/{Path(f['path']).name}", f.pop("path"))
        review["_figures"] = figures
        review["_page1"] = storage.put_file(f"figures/{pid}/page1.png", tmp / "figs" / "page1.png")
    review["_cost"] = {"total_usd": 0.0, "calls": [], "note": "demo data - no API calls"}
    card_keys = cardnews.render_and_store({**SAMPLE_PAPER, "id": pid}, review) if render_cards else []
    db.update_paper(pid, status="done", read_depth="full_pdf", review=review, relevance=9.0,
                    relevance_reason="관심 키워드와 직접 관련된 데모 논문",
                    score_method=review["methodology_score"], score_novelty=review["novelty_score"],
                    score_overall=review["overall_score"], card_paths=card_keys, cost_usd=0.0)
    for uid, title, status, rel, reason in QUEUED:
        db.insert_paper({**SAMPLE_PAPER, "uid": uid, "title": title, "matched_keywords": SAMPLE_PAPER["matched_keywords"][:1]})
        db.update_paper(db.get_paper_by_uid(uid)["id"], status=status, relevance=rel, relevance_reason=reason)
    return pid

