# app/services/evidence/claims.py
"""답변을 주장(문장) 단위로 나누고, 통제 변형에 새로 넣은 값이 문단에 없는지 검사한다."""
from __future__ import annotations

import re

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
