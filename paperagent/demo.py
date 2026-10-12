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
    "one_liner": "생성 → 검증 → 수정 루프만 붙여 7B 모델의 MATH 정답률을 41.2%에서 53.6%로 올렸다",
    "tldr": (
        "작은 언어모델은 풀이 도중 한 단계에서 실수하면 그 오류를 끝까지 끌고 가 틀린 답을 내는 경우가 많습니다. "
        "기존 해법인 다수결(Self-Consistency)은 답을 여러 번 뽑아 투표할 뿐 틀린 단계를 직접 고치지 못하고, "
        "별도 검증 모델을 쓰는 방법은 추가 학습과 모델이 필요합니다. "
        "이 논문은 같은 모델이 답을 만든 뒤 풀이를 한 줄씩 다시 검증하고, 틀린 단계를 찾으면 그 지점부터 다시 푸는 루프를 제안합니다. "
        "추가 학습이나 외부 검증기는 없습니다. "
        "7B 모델 기준 MATH 정답률이 41.2%에서 53.6%로 12.4%p 올랐고, 같은 비용의 Self-Consistency(42.4%)보다 크게 앞섰습니다. "
        "GSM8K, ARC-Challenge, StrategyQA에서도 2~6%p 개선됐으며, 검증을 4회까지 반복할 때 효과가 커지고 그 이후로는 거의 늘지 않습니다. "
        "대신 추론 비용이 약 3배로 늘고 시드별 분산은 보고되지 않았습니다. "
        "큰 모델을 쓰기 어려운 온디바이스·저비용 환경에서 추론 품질을 끌어올리려는 팀에 유용합니다."
    ),
    "key_points": [
        "추가 학습 없이 같은 모델로 '생성 → 검증 → 수정'을 반복하는 추론 방법",
        "7B 모델의 MATH 정답률 41.2% → 53.6% (+12.4%p, Table 1)",
        "같은 추론 비용의 Self-Consistency(42.4%)보다 11.2%p 높음",
        "GSM8K·ARC-Challenge·StrategyQA에서도 2~6%p 개선",
        "검증 4회까지 효과가 커지고 이후엔 포화 (Fig. 2)",
        "한계: 추론 비용 약 3배, 시드별 분산 미보고",
    ],
    "background": (
        "수학·논리 추론에서 언어모델은 여러 단계의 풀이를 차례로 만들어 냅니다. 큰 모델은 중간에 실수를 해도 스스로 알아채는 경우가 있지만, "
        "7B 안팎의 작은 모델은 한 번 틀린 단계를 그대로 믿고 이어 가는 경향이 강합니다. 지금까지는 답을 여러 번 뽑아 다수결하는 "
        "Self-Consistency나, 따로 학습한 검증 모델(verifier)로 답을 고르는 방법이 주로 쓰였습니다."
    ),
    "problem": (
        "작은 모델이 추론 도중 저지른 단계별 오류를 추가 학습이나 외부 모델 없이 스스로 찾아 고칠 수 있는지가 이 논문의 질문입니다. "
        "다수결은 틀린 단계를 고치지 못하고, 검증 모델은 학습 비용과 추가 모델이 필요하다는 한계가 있습니다."
    ),
    "key_idea": (
        "답을 '맞히는 것'보다 주어진 풀이가 '맞는지 확인하는 것'이 작은 모델에게도 더 쉽다는 점을 이용합니다. "
        "같은 모델에게 검증을 맡기고, 틀린 단계부터 다시 풀게 하면 오류가 끝까지 전파되지 않습니다."
    ),
    "method": (
        "질문이 들어오면 모델이 단계별 풀이와 답을 먼저 생성합니다. 이어서 같은 모델이 검증 프롬프트로 풀이를 한 줄씩 다시 읽으며 "
        "각 단계가 맞는지 판단합니다. 틀린 단계가 발견되면 그 앞까지의 풀이는 유지하고, 해당 단계부터 다시 풀도록 수정 프롬프트를 줍니다. "
        "이 과정을 검증이 통과하거나 최대 횟수에 도달할 때까지 반복합니다. 별도 검증 모델, 추가 학습, 외부 도구는 쓰지 않습니다."
    ),
    "method_steps": [
        {"title": "생성", "detail": "질문에 대해 단계별 풀이(chain-of-thought)와 최종 답을 만듭니다."},
        {"title": "단계별 검증", "detail": "같은 모델이 풀이를 한 줄씩 다시 읽고 각 단계가 맞는지 '맞음/틀림'으로 판단합니다."},
        {"title": "부분 수정", "detail": "처음 틀린 단계 앞까지는 유지하고, 그 단계부터 다시 풀어 새 답을 만듭니다."},
        {"title": "반복", "detail": "검증을 통과하거나 최대 횟수(실험에선 0~4회)에 도달하면 멈추고 마지막 답을 냅니다."},
    ],
    "setup": [
        {"label": "데이터셋", "value": "MATH, GSM8K, ARC-Challenge, StrategyQA (각 공식 테스트 세트)"},
        {"label": "모델", "value": "7B 오픈소스 언어모델 1종 (추가 학습 없음)"},
        {"label": "비교 대상", "value": "기본 chain-of-thought, 같은 비용의 Self-Consistency (다수결)"},
        {"label": "평가 지표", "value": "정답률(accuracy), 추론 비용(생성 토큰 수 기준)"},
        {"label": "검증 횟수", "value": "0~4회로 바꿔 가며 효과 측정"},
    ],
    "results": [
        {"item": "MATH 정답률", "value": "53.6%", "comparison": "기본 CoT 41.2% (+12.4%p)", "source": "Table 1"},
        {"item": "MATH, 같은 비용 비교", "value": "53.6%", "comparison": "Self-Consistency 42.4%", "source": "Table 1"},
        {"item": "GSM8K 정답률", "value": "+6%p", "comparison": "기본 CoT 대비", "source": "Table 2"},
        {"item": "ARC-Challenge · StrategyQA", "value": "+2~4%p", "comparison": "기본 CoT 대비", "source": "Table 2"},
        {"item": "추론 비용", "value": "약 3배", "comparison": "기본 CoT 대비 생성 토큰", "source": "Sec. 5"},
    ],
    "analysis": [
        "검증 횟수를 0→4회로 늘리면 MATH 정답률이 41.2 → 47.8 → 51.3 → 53.1 → 53.6%로 오르고 4회 이후엔 거의 변화가 없습니다 (Fig. 2).",
        "Self-Consistency는 표본 수를 늘려도 42.4% 근처에서 멈춰, 단순히 더 많이 생성하는 것만으로는 같은 효과가 나지 않습니다.",
        "수정 단계에서 처음부터 다시 풀게 하면 효과가 줄어, '틀린 단계부터' 고치는 설계가 중요하다고 보고합니다.",
    ],
    "contributions": [
        "추가 학습 없이 붙일 수 있는 자기 검증·부분 수정 루프를 제안",
        "검증 횟수와 정답률·비용의 관계를 체계적으로 분석",
        "같은 비용의 다수결보다 낫다는 점을 비교 실험으로 보임",
    ],
    "limitations": [
        "추론 비용이 기본 대비 약 3배로 늘어나, 비용을 줄이는 방향의 보완이 필요해 보입니다.",
        "실험 모델이 7B 한 종류라 다른 크기·계열에서도 같은 효과가 나는지는 추가 연구가 필요할 수 있습니다.",
        "시드별 분산과 통계적 유의성 보고는 다소 미흡할 수 있습니다.",
        "코드 생성이나 일반 대화 과제로의 확장은 추가 검증이 필요해 보입니다.",
    ],
    "prior_work": (
        "가장 가까운 연구는 Self-Consistency와 학습된 검증 모델(verifier) 방식입니다. "
        "이 논문은 투표 대신 단계별로 오류를 찾아 고친다는 점, 그리고 검증 모델 없이 같은 모델 하나로 해결한다는 점에서 다릅니다."
    ),
    "why_it_matters": (
        "큰 모델을 쓰기 어려운 온디바이스·저비용 환경에서, 학습 없이 프롬프트 설계만으로 작은 모델의 추론 품질을 끌어올릴 수 있음을 보여 줍니다. "
        "비용 대비 효과를 고려해 검증 횟수를 조절할 수 있다는 점도 실무에 유용합니다."
    ),
    "who_should_read": "온디바이스나 저비용 환경에서 LLM 추론 품질을 높이려는 엔지니어와 추론 기법 연구자",
    "glossary": [
        {"term": "Chain-of-thought (CoT)", "meaning": "답만 내지 않고 중간 풀이 단계를 글로 쓰게 하는 프롬프트 방식"},
        {"term": "Self-Consistency", "meaning": "같은 질문에 답을 여러 번 생성한 뒤 가장 많이 나온 답을 고르는 다수결 방식"},
        {"term": "Verifier (검증 모델)", "meaning": "풀이나 답이 맞는지 판단하도록 따로 학습한 모델"},
        {"term": "MATH", "meaning": "경시대회 수준 수학 문제로 구성된 추론 벤치마크"},
    ],
    "tags": ["LLM", "Reasoning", "Self-verification", "Inference"],
    "methodology_score": 7.5,
    "methodology_reason": "베이스라인과 벤치마크 구성이 충실합니다. 시드별 분산과 다양한 모델 크기에 대한 검증이 보완되면 더 설득력 있을 것 같습니다.",
    "novelty_score": 8.0,
    "novelty_reason": "자기 검증 아이디어 자체는 알려져 있지만, 학습 없이 작은 모델에서 일관된 이득을 보인 점이 새롭습니다.",
    "strengths": ["추가 학습 없이 바로 적용 가능", "같은 비용의 다수결보다 큰 개선", "검증 횟수별 효과를 체계적으로 분석"],
    "weaknesses": ["추론 비용(약 3배)을 줄이는 방향의 보완이 필요해 보입니다", "여러 모델 크기와 반복 실험에 대한 추가 검증이 필요할 수 있습니다"],
    "verdict": "작은 모델로 추론 품질을 끌어올려야 하는 팀이라면 읽어 볼 만합니다.",
    "read_confidence": "high",
    "overall_score": 7.8,
    "card": {
        "category": "LLM 추론",
        "cover_headline": "작은 AI가\n스스로 답을 검산합니다",
        "cover_sub": "답을 낸 뒤 스스로 다시 확인하고 고치게 했더니, 수학 문제 정답률이 크게 올랐습니다.",
        "cover_points": [
            "답 만들기 → 확인하기 → 고치기를 한 AI가 반복",
            "수학 문제 정답률 41.2% → 53.6%로 상승",
            "대신 답을 내는 데 드는 계산은 약 3배",
        ],
        "problem": {
            "headline": "작은 AI는 왜\n수학 풀이에 약할까요?",
            "points": [
                "작은 언어모델*은 풀이 중간에 한 번 틀리면, 그 실수를 끝까지 이어 가 틀린 답을 냅니다.",
                "여러 번 풀게 한 뒤 가장 많이 나온 답을 고르는 방법도, 틀린 단계 자체를 고치지는 못합니다.",
                "따로 채점용 AI를 두는 방법은 그 AI를 새로 학습시켜야 해서 비용이 큽니다.",
            ],
            "context": "AI는 수학 문제를 풀 때 풀이 과정을 한 줄씩 써 내려갑니다. 큰 AI는 중간 실수를 스스로 알아채기도 하지만, 작은 AI는 그대로 믿고 이어 가는 경우가 많습니다.",
            "figure_number": 0,
        },
        "idea": {
            "headline": "답을 낸 뒤\n스스로 다시 확인합니다",
            "body": "문제를 처음부터 맞히는 것보다, 이미 쓴 풀이가 맞는지 확인하는 일이 더 쉽다는 점을 이용합니다. 같은 AI가 자기 풀이를 한 줄씩 점검하고, 틀린 줄부터 다시 풉니다.",
            "figure_number": 1,
        },
        "steps": [
            {"title": "풀이 쓰기", "detail": "문제를 받으면 풀이 과정을 단계별로 쓰고 최종 답을 냅니다."},
            {"title": "한 줄씩 점검", "detail": "같은 AI가 자기 풀이를 한 줄씩 다시 읽으며 각 단계가 맞는지 판단합니다."},
            {"title": "틀린 곳부터 고치기", "detail": "처음 틀린 줄 앞까지는 그대로 두고, 그 줄부터 다시 풀어 새 답을 만듭니다."},
            {"title": "반복", "detail": "점검을 통과하거나 정해 둔 횟수(최대 4번)에 이르면 마지막 답을 냅니다."},
        ],
        "setup": [
            {"label": "문제 세트", "value": "MATH*, GSM8K 등 수학·추론 시험 문제 4종"},
            {"label": "사용한 AI", "value": "70억 개 파라미터* 크기의 공개 언어모델 1종 (추가 학습 없음)"},
            {"label": "비교 대상", "value": "그냥 한 번 풀게 한 경우, 같은 계산량으로 여러 번 풀어 다수결한 경우"},
            {"label": "평가 기준", "value": "정답률, 답을 내는 데 쓴 계산량"},
        ],
        "findings": [
            "점검을 0번에서 4번까지 늘릴수록 정답률이 올랐고, 4번이 넘으면 거의 그대로였습니다.",
            "같은 계산량으로 다수결만 한 경우는 42.4%에 머물러, '틀린 곳부터 고치기'가 핵심이었습니다.",
        ],
        "results": {
            "headline": "정답률이\n크게 올랐습니다",
            "highlight": "+12.4%p",
            "highlight_label": "MATH* 정답률 상승",
            "rows": [
                {"item": "MATH 정답률", "value": "53.6%", "note": "기존 41.2% → 100문제 중 약 12문제 더 맞힘"},
                {"item": "다수결 방식", "value": "42.4%", "note": "같은 계산량으로 비교했을 때"},
                {"item": "다른 시험 3종", "value": "+2~6%p", "note": "모두 정답률이 올랐습니다"},
            ],
            "figure_number": 2,
        },
        "verdict": {
            "strengths": ["추가 학습 없이 기존 AI에 바로 적용할 수 있습니다", "같은 계산량의 다수결보다 정답률이 11.2%p 높습니다"],
            "weaknesses": ["계산량이 약 3배로 늘어, 비용을 줄이는 보완이 필요해 보입니다", "다른 크기의 AI에서도 효과가 있는지 추가 연구가 필요할 수 있습니다"],
            "who": "작고 저렴한 AI로 더 정확한 답을 얻고 싶은 분",
        },
        "summary_rows": [
            {"label": "문제", "sub": "배경", "value": "작은 AI의 풀이 실수", "value_sub": "한 번 틀리면 끝까지 틀림"},
            {"label": "방법", "sub": "핵심", "value": "쓰고 → 점검하고 → 고치기", "value_sub": "같은 AI가 반복, 추가 학습 없음"},
            {"label": "데이터", "sub": "평가", "value": "수학·추론 시험 4종", "value_sub": "MATH · GSM8K · ARC · StrategyQA"},
            {"label": "결과", "sub": "성능", "value": "정답률 41.2→53.6%", "value_sub": "다수결(42.4%)보다 높음"},
            {"label": "보완할 점", "sub": "알아 둘 점", "value": "계산량 약 3배", "value_sub": "여러 모델에서의 추가 검증 필요"},
        ],
        "terms": [
            {"term": "언어모델", "meaning": "많은 글을 학습해, 앞의 내용에 이어질 말을 만들어 내는 AI. 챗GPT 같은 챗봇이 이 방식으로 동작합니다"},
            {"term": "MATH", "meaning": "고등학교~경시대회 수준 수학 문제 1만 2천여 개로 AI의 풀이 실력을 재는 시험"},
            {"term": "파라미터", "meaning": "AI가 학습하며 조정하는 숫자들. 많을수록 모델이 크고 계산이 많이 듭니다"},
        ],
        "caption": (
            "작은 AI도 스스로 답을 검산하면 훨씬 잘 풉니다.\n\n"
            "작은 AI는 수학 풀이 중 한 번 틀리면 그 실수를 끝까지 이어 가는 경우가 많아요.\n"
            "이 논문은 AI가 답을 낸 뒤 자기 풀이를 한 줄씩 점검하고, 틀린 줄부터 다시 풀게 했습니다. 추가 학습은 필요 없어요.\n\n"
            "📈 수학 문제 정답률 41.2% → 53.6% (100문제 중 약 12문제 더 맞힘)\n"
            "📈 같은 계산량으로 여러 번 풀어 다수결한 경우(42.4%)보다 높음\n"
            "📈 다른 추론 시험 3종에서도 2~6%p 개선\n\n"
            "💡 계산량이 약 3배로 늘어, 이를 줄이는 보완이 필요해 보입니다.\n"
            "👉 작고 저렴한 AI로 더 정확한 답을 얻고 싶은 분께 추천해요."
        ),
        "hashtags": ["#LLM", "#생성형AI"],
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
                    score_overall=review["overall_score"], card_paths=card_keys, cost_usd=0.0,
                    reviewed_at=db.now_iso())
    for uid, title, status, rel, reason in QUEUED:
        db.insert_paper({**SAMPLE_PAPER, "uid": uid, "title": title, "matched_keywords": SAMPLE_PAPER["matched_keywords"][:1]})
        db.update_paper(db.get_paper_by_uid(uid)["id"], status=status, relevance=rel, relevance_reason=reason)
    return pid

