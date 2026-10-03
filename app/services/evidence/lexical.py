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
_CORP_MARK = re.compile(r"㈜|\(주\)|주식회사")
# 명사 뒤 조사·서술격(긴 것부터). 위치와 무관하게 이 꼬리가 붙은 토큰의 어간을 후보로 본다
_TAILS = ("으로부터", "로부터", "에게서", "입니다", "이었다", "에서는", "에게", "에서", "으로", "이다", "이며", "였다",
          "와의", "과의", "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "로", "도", "다")
_HANGUL = re.compile(r"[가-힣]{2,}")
_LATIN_STEM = re.compile(r"[A-Za-z0-9가-힣][\w&.\-]*")
# 어간이 이 글자로 끝나면 서술형(생산하는·공급한다·체결했다 등)으로 보고 뺀다
_VERBAL = ("하", "되", "있", "없", "않", "했", "됐", "한", "된", "었", "았", "였", "겠", "졌", "할", "될", "니")
# 회사명이 아닌 일반명사(문단에 그대로 없어도 다른 회사를 가리키지 않는다)
GENERIC_NOUNS = ("당사", "회사", "동사", "자사", "본사", "매출", "매출액", "매출처", "제품", "사업", "부문", "고객", "고객사",
                 "거래처", "공급처", "협력사", "경쟁사", "시장", "기업", "그룹", "계열사", "자회사", "종속회사", "지배회사",
                 "연결회사", "이익", "영업이익", "순이익", "비중", "원재료", "설비", "공장", "생산", "판매", "수출", "수입",
                 "연구", "개발", "계약", "기술", "투자", "위험", "환율", "금리", "주요", "국내", "해외", "전년", "당기", "전기",
                 "비용", "가격", "수요", "공급", "서비스", "브랜드", "업체", "주주", "최대주주", "사업부", "법인", "지역")
_BOUND = r"[가-힣A-Za-z0-9]"
_TAIL_RE = "|".join(sorted(_TAILS + ("만", "및"), key=len, reverse=True))


def _stem(tok: str) -> str | None:
    """꼬리(조사·서술격)를 뗀 어간. 꼬리가 없으면 None."""
    for t in _TAILS:
        if tok.endswith(t) and len(tok) > len(t):
            return tok[:-len(t)]
    return None


def name_candidates(claim: str, company: str) -> set[str]:
    """주장에서 회사명 후보를 모은다(위치 무관): 선택 회사명(주장에 있을 때), 따옴표 안 이름, ㈜·(주)·주식회사 토큰,
    라틴 대문자 토큰, 조사·서술격(GENERIC_NOUNS 제외, 서술형 어간 제외)이 붙은 2자 이상 한글 명사형 토큰."""
    out = {company} if company and company in claim else set()
    out |= {m.strip() for m in _QUOTED.findall(claim)}
    for raw in claim.split():
        if _CORP_MARK.search(raw):
            tok = _EDGE.sub("", _CORP_MARK.sub("", raw))
            tok = _stem(tok) or tok
            if _LATIN_STEM.fullmatch(tok) and len(tok) >= 2:
                out.add(tok)
            continue
        tok = _EDGE.sub("", raw)
        if re.search(r"[A-Z]", tok):
            out.add((_stem(tok) or tok) if re.search(r"[가-힣]$", tok) else tok)
            continue
        stem = _stem(tok)
        if stem and _HANGUL.fullmatch(stem) and not stem.endswith(_VERBAL) and stem not in GENERIC_NOUNS:
            out.add(stem)
    return out


def _in_passage(name: str, passage: str) -> bool:
    """이름이 문단에 토큰 경계로 나오는지(뒤에 조사가 붙는 것은 허용). '삼성'은 '삼성전자'에 걸리지 않는다."""
    pat = rf"(?<!{_BOUND}){re.escape(name)}(?=(?:{_TAIL_RE}){{0,2}}(?!{_BOUND}))"
    return re.search(pat, passage) is not None


def names_in_passage(claim: str, passage: str, company: str) -> bool:
    """주장의 회사명 후보가 모두 문단에 토큰 경계로 그대로 있으면 참(후보가 없으면 참)."""
    return all(_in_passage(n, passage) for n in name_candidates(claim, company))


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
