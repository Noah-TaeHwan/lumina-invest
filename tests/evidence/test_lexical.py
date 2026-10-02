# tests/evidence/test_lexical.py
"""제품 1차 필터(app)가 평가 기준선(lab)과 같은 값을 내는지 고정 예제로 대조한다. lab import는 테스트에서만 한다."""
import pytest

from app.services.evidence import lexical
from lab.evidence import baselines

P = [
    "회사는 메모리 반도체(DRAM, NAND)와 스마트폰을 생산·판매한다.",
    "[표: 부문별 매출 | 단위 백만원] DX 부문 매출액 174,887,683 DS 부문 매출액 111,066,059",
    "2025년 영업이익은 32조 7,260억원으로 전년 대비 증가하였다.",
    "주요 원재료는 웨이퍼이며 국내외 협력사에서 조달한다.",
    "당사의 연구개발비는 매출액의 10.9% 수준이다.",
]
CASES = [
    ("회사는 메모리 반도체를 생산한다.", P),
    ("DX 부문 매출액은 174,887,683백만원이다.", P),
    ("DX 부문 매출액은 999,999,999백만원이다.", P),
    ("2025년 영업이익은 32조 7,260억원이다.", P),
    ("2025년 영업이익은 40조원이다.", P),
    ("주요 원재료는 웨이퍼다.", P),
    ("연구개발비는 매출액의 10.9%다.", P),
    ("연구개발비는 매출액의 20%다.", P),
    ("LG화학이 배터리를 만든다.", P),
    ("", P),
    ("  ", P),
    ("a", P),
    ("스마트폰을 판매한다", P[:1]),
    ("스마트폰을 판매한다", P[1:]),
    ("반도체 반도체 반도체", P),
    ("회사는 메모리 반도체(DRAM, NAND)와 스마트폰을 생산·판매한다.", P),
    ("웨이퍼는 협력사에서 조달한다.", [P[3], P[0]]),
    ("DS 부문 매출액 111,066,059", P[1:2]),
    ("영업이익이 증가하였다.", P[2:3]),
    ("전혀 관계없는 문장입니다.", P),
]


def test_has_twenty_fixed_examples():
    assert len(CASES) == 20


@pytest.mark.parametrize("claim,passages", CASES)
def test_lex_score_equals_lab_baseline(claim, passages):
    assert lexical.char_bigrams(claim) == baselines.char_bigrams(claim)
    assert lexical.lex_score(claim, passages) == baselines.lex_score(claim, passages)


@pytest.mark.parametrize("claim,passages", CASES)
def test_lex_best_returns_score_and_source(claim, passages):
    score, idx = lexical.lex_best(claim, passages)
    assert score == baselines.lex_score(claim, passages)
    if score > 0:
        assert idx is not None and lexical.lex_score(claim, [passages[idx]]) == score
    else:
        assert idx is None


def test_lexical_module_does_not_import_lab():
    import inspect
    src = inspect.getsource(lexical)
    assert "from lab" not in src and "import lab" not in src
