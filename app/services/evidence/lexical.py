# app/services/evidence/lexical.py
"""1차 필터용 어휘 겹침 점수(A-2 spec 4-3·6절).

lab/evidence/baselines.py의 char_bigrams·lex_score를 의도적으로 복제했다(제품 코드는 lab/을 import하지 않는다).
같은 입력에 같은 값을 내는지는 tests/evidence/test_lexical.py가 고정 예제로 대조한다.
JEV 출력으로 학습한 것이 아니라 AI 참조 라벨 기준으로 구간을 정하는 규칙의 입력이다.

1차 필터 구간(lex_features·tier_route)은 제품 실행기(runner)와 A-2 평가(lab/evidence/a2)가 같은 함수를 쓴다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

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


# 회사명 후보(spec 6.1절 상단 구간 조건). 형태소 분석 없이 넓게 잡는다: 후보가 많으면 상단 구간이 좁아져
# JEV로 넘어갈 뿐이고, 놓치면 주체 교체 문장이 JEV 없이 ✅가 된다.
_EDGE = re.compile(r"^[^\w]+|[^\w]+$")
_QUOTED = re.compile(r"['‘\"“「『]([^'’\"”」』]{2,})['’\"”」』]")
_TOPIC = re.compile(r"(?:은|는|이|가)$")
_FIRST = re.compile(r"(?:은|는|이|가|의)$")
_PARTICLES = re.compile(r"(?:으로|에서|은|는|이|가|을|를|의|에|도|와|과|로)$")
_CORP_MARK = re.compile(r"㈜|\(주\)|주식회사")
_STEM = re.compile(r"[가-힣A-Za-z0-9]{2,}")
_VERBAL = ("하", "되", "있", "없", "않", "했", "됐", "한", "된")


def name_candidates(claim: str, company: str) -> set[str]:
    """주장에서 회사명 후보를 모은다: 선택 회사명, 라틴 대문자 토큰, 따옴표 안 이름, 법인 표지 토큰,
    주제·주격 조사(은·는·이·가, 첫 토큰은 의 포함)가 붙은 2자 이상 명사형 토큰(서술형 어간은 뺀다)."""
    out = {company} if company and company in claim else set()
    out |= {m.strip() for m in _QUOTED.findall(claim)}
    for i, raw in enumerate(claim.split()):
        if _CORP_MARK.search(raw):
            tok = _PARTICLES.sub("", _EDGE.sub("", _CORP_MARK.sub("", raw)))
            if _STEM.fullmatch(tok):
                out.add(tok)
            continue
        tok = _EDGE.sub("", raw)
        if re.search(r"[A-Z]", tok):
            out.add(_PARTICLES.sub("", tok) if re.search(r"[가-힣]$", tok) else tok)
            continue
        m = (_FIRST if i == 0 else _TOPIC).search(tok)
        stem = tok[:m.start()] if m else ""
        if _STEM.fullmatch(stem) and not stem.endswith(_VERBAL):
            out.add(stem)
    return out


def names_in_passage(claim: str, passage: str, company: str) -> bool:
    """주장의 회사명 후보가 모두 문단에 그대로 있으면 참(후보가 없으면 참)."""
    return all(n in passage for n in name_candidates(claim, company))


@dataclass(frozen=True)
class LexFeatures:
    """1차 필터 입력. high_ok: 최고 점수 문단이 숫자 확인과 회사명 조건을 함께 통과한다."""

    lex: float
    best: int | None
    high_ok: bool


def lex_features(claim: str, passages: list[str], company: str) -> LexFeatures:
    """어휘 겹침 점수, 그 문단 번호, 상단 구간 조건 통과 여부."""
    lex, best = lex_best(claim, passages)
    ok = best is not None and number_check(claim, passages[best]) and names_in_passage(claim, passages[best], company)
    return LexFeatures(lex, best, ok)


def tier_route(lex: float, high_ok: bool, theta_low: float | None, theta_high: float | None) -> str:
    """1차 필터 경로. 하단(lex < θ_low)이 먼저, 상단(lex ≥ θ_high이고 high_ok), 나머지는 jev."""
    if theta_low is not None and lex < theta_low:
        return "lex_low"
    if theta_high is not None and high_ok and lex >= theta_high:
        return "lex_high"
    return "jev"
