# app/services/evidence/claims.py
"""답변을 주장(문장) 단위로 나누고, 통제 변형에 새로 넣은 값이 문단에 없는지 검사한다.

제품(A-2)용으로 원문 오프셋을 함께 돌려주는 claim_spans와 비주장 규칙(A-2 spec 5.2절)을 둔다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_BULLET = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s*")
_SPLIT = re.compile(r"(?<=[.!?])\s+")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def split_sentences(text: str) -> list[str]:
    """줄바꿈과 문장부호로 나누고 글머리표를 뗀다. 4자 미만 조각은 버린다."""
    out = []
    for line in text.splitlines():
        line = _BULLET.sub("", line).strip()
        for s in _SPLIT.split(line):
            s = s.strip()
            if len(s) >= 4:
                out.append(s)
    return out


@dataclass(frozen=True)
class Span:
    """답변 원문 안의 문장 하나. text == 원문[start:end]."""

    text: str
    start: int
    end: int


def claim_spans(text: str) -> list[Span]:
    """split_sentences와 같은 규칙으로 나누고 원문 오프셋을 붙인다(결과 문장 목록이 같다)."""
    out = []
    pos = 0
    for raw in text.splitlines(keepends=True):
        line = raw.splitlines()[0] if raw.splitlines() else ""
        m = _BULLET.match(line)
        rest, base = (line[m.end():], pos + m.end()) if m else (line, pos)
        body = rest.strip()
        base += len(rest) - len(rest.lstrip())
        cut = 0
        for piece_end, next_start in [(sm.start(), sm.end()) for sm in _SPLIT.finditer(body)] + [(len(body), len(body))]:
            piece = body[cut:piece_end]
            s = piece.strip()
            if len(s) >= 4:
                start = base + cut + len(piece) - len(piece.lstrip())
                out.append(Span(s, start, start + len(s)))
            cut = next_start
        pos += len(raw)
    return out


NOT_CLAIM_PHRASES = ("문단에", "제공된 정보", "확인할 수 없", "알 수 없", "언급되어 있지 않", "추가 정보가 필요")
NOT_CLAIM_MAX_SHORT = 10
# 목록 머리말("…는 다음과 같습니다.", "…:"). 숫자가 든 문장은 주장으로 남긴다(머리말이 값을 함께 말할 수 있다)
NOT_CLAIM_LEAD_ENDS = ("다음과 같습니다", "다음과 같다", "아래와 같습니다", "아래와 같다")
NOT_CLAIM_LEAD_COLON = ":"
_PARTICLE = re.compile(r"(?:은|는|이|가|을|를|의|에|도|와|과|로|으로|에서)$")
_PREDICATE_END = ("다", "요", "죠", "까", "니", "네")
_TOKEN_EDGE = re.compile(r"^[^\w]+|[^\w]+$")


def _proper_noun_candidate(sentence: str) -> bool:
    """형태소 분석 없이 고유명사 후보를 근사한다: 라틴 대문자 토큰, 또는 끝 조사를 뗀 2자 이상 한글 토큰 중
    서술 어미(다·요·죠·까·니·네)로 끝나지 않는 것."""
    for tok in sentence.split():
        tok = _TOKEN_EDGE.sub("", tok)
        if re.search(r"[A-Z]", tok):
            return True
        stem = _PARTICLE.sub("", tok) if len(tok) > 2 else tok
        if len(stem) >= 2 and re.fullmatch(r"[가-힣]+", stem) and not stem.endswith(_PREDICATE_END):
            return True
    return False


def is_not_claim(sentence: str) -> bool:
    """A-2 spec 5.2절 비주장 규칙: 물음표로 끝남, 답변 불가·자료 언급 표현, 숫자 없는 목록 머리말,
    숫자·고유명사 후보 없는 10자 미만."""
    s = sentence.strip()
    if s.endswith(("?", "？")):
        return True
    if any(p in s for p in NOT_CLAIM_PHRASES):
        return True
    if re.search(r"\d", s):
        return False
    if s.endswith((NOT_CLAIM_LEAD_COLON, "：")) or s.rstrip(".").endswith(NOT_CLAIM_LEAD_ENDS):
        return True
    return len(s) < NOT_CLAIM_MAX_SHORT and not _proper_noun_candidate(s)


def numeric_tokens(text: str) -> set[str]:
    """숫자 표기(쉼표 제거)를 모은다. 단위 환산은 하지 않는다(Stage 1 숫자 대조에서 한다)."""
    return {t.replace(",", "") for t in _NUM.findall(text)}


def new_values_absent(variant: str, source: str, passages: list[str], names: tuple[str, ...] = ()) -> bool:
    """변형이 원문 참 문장에 없던 숫자·회사명을 넣었다면, 그 값이 문단 어디에도 없어야 한다.

    부분 문자열로 보수적으로 검사한다(예: '12'가 '2012' 안에 있어도 있음으로 본다).
    """
    blob = " ".join(passages).replace(",", "")
    if any(n in blob for n in numeric_tokens(variant) - numeric_tokens(source)):
        return False
    return not any(n in variant and n not in source and n in blob for n in names)
