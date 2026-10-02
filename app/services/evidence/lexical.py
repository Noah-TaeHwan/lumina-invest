# app/services/evidence/lexical.py
"""1차 필터용 어휘 겹침 점수(A-2 spec 4-3·6절).

lab/evidence/baselines.py의 char_bigrams·lex_score를 의도적으로 복제했다(제품 코드는 lab/을 import하지 않는다).
같은 입력에 같은 값을 내는지는 tests/evidence/test_lexical.py가 고정 예제로 대조한다.
JEV 출력으로 학습한 것이 아니라 AI 참조 라벨 기준으로 구간을 정하는 규칙의 입력이다.
"""
from __future__ import annotations

import re

from app.services.evidence.numbers import number_check

_WS = re.compile(r"\s+")


def char_bigrams(text: str) -> set[str]:
    """공백을 지운 글자 바이그램(한국어 조사 변화에 덜 민감한 어휘 단위)."""
    t = _WS.sub("", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def lex_score(claim: str, passages: list[str]) -> float:
    """B-lex: 숫자 확인을 통과한 문단에서 주장 바이그램 재현율의 최댓값."""
    return lex_best(claim, passages)[0]


def lex_best(claim: str, passages: list[str]) -> tuple[float, int | None]:
    """lex_score와 같은 값과 그 값을 낸 첫 문단 번호(0 기준). 점수가 0이면 번호는 None."""
    cb = char_bigrams(claim)
    if not cb:
        return 0.0, None
    best, idx = 0.0, None
    for i, p in enumerate(passages):
        v = len(cb & char_bigrams(p)) / len(cb) if number_check(claim, p) else 0.0
        if v > best:
            best, idx = v, i
    return best, idx
