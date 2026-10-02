# app/services/evidence/numbers.py
"""주장의 숫자가 문단에 같은 값으로 있는지 코드로 확인한다(spec 3절, 필요조건).

같은 값이란 문단 값을 주장 표기의 마지막 자리 단위(step)로 반올림하거나 버림했을 때 주장 값과 같아지는 것이다
(예: 1조 67억 ← 1,006,771백만은 버림으로 일치, 2조 ← 1,006,771백만은 불일치). 기간·지표가 맞는지는 판단하지 않는다(JEV 몫).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

_UNIT = {"조": 10**12, "십억": 10**9, "억": 10**8, "천만": 10**7, "백만": 10**6, "만": 10**4, "천": 10**3}
_TOKEN = re.compile(r"(\()?(\d[\d,]*(?:\.\d+)?)\s*(조|십억|억|천만|백만|만|천)?\s*(%p|%포인트|%|퍼센트)?")
_SEGMENT = re.compile(r"(?=\[[^\]]*표[^\]]*\])")
_TABLE_UNIT = re.compile(r"\[[^\]]*표[^\]]*단위 ([^\]]+)\]")


@dataclass(frozen=True)
class Num:
    """정규화한 숫자: 값, 허용 범위(마지막 자리 단위), 종류(abs·pct·pctp)."""

    value: Decimal
    step: Decimal
    kind: str


def _scale(unit_text: str) -> tuple[int, str]:
    """표 단위 문자열을 배수와 기본 종류로 바꾼다(예: '백만원' → 10^6, '%' → 퍼센트)."""
    u = unit_text.strip()
    if u.startswith("%"):
        return 1, "pct"
    for k, v in _UNIT.items():
        if u.startswith(k):
            return v, "abs"
    return 1, "abs"


def parse(text: str, default_scale: int = 1, default_kind: str = "abs") -> list[Num]:
    """숫자 표현을 차례로 뽑는다. '1조 2,345억'처럼 큰 단위 뒤 작은 단위는 하나로 합친다."""
    out: list[Num] = []
    prev_mult, prev_end = 0, -1
    for m in _TOKEN.finditer(text):
        paren, digits, unit, pct = m.groups()
        raw = digits.replace(",", "").rstrip(".")
        if not raw or not raw.replace(".", "", 1).isdigit():
            continue
        val = Decimal(raw)
        dec = -val.as_tuple().exponent if "." in raw else 0
        if pct in ("%p", "%포인트"):
            kind, mult = "pctp", 1
        elif pct:
            kind, mult = "pct", 1
        elif unit:
            kind, mult = "abs", _UNIT[unit]
        else:
            kind, mult = default_kind, (default_scale if default_kind == "abs" else 1)
        v = val * mult
        step = Decimal(1).scaleb(-dec) * mult
        if paren and text[m.end(2):m.end(2) + 1] == ")":
            v = -v
        if unit and out and prev_mult > mult and kind == "abs" and text[prev_end:m.start()].strip() == "":
            v = out.pop().value + v
        out.append(Num(v, step, kind))
        prev_mult, prev_end = (_UNIT[unit] if unit else 0), m.end()
    return out


def passage_numbers(text: str) -> list[Num]:
    """문단을 표 행 접두어 단위로 나눠, 조각마다 표 단위를 적용해 숫자를 뽑는다."""
    nums: list[Num] = []
    for seg in _SEGMENT.split(text):
        m = _TABLE_UNIT.match(seg)
        scale, kind = _scale(m.group(1)) if m else (1, "abs")
        body = seg[m.end():] if m else seg
        nums += parse(body, scale, kind)
    return nums


def _same(h: Num, w: Num) -> bool:
    """문단 값 h를 주장 w의 마지막 자리 단위로 반올림 또는 버림했을 때 w와 같은가."""
    if h.kind != w.kind:
        return False
    q, target = h.value / w.step, w.value / w.step
    return q.to_integral_value(ROUND_HALF_UP) == target or q.to_integral_value(ROUND_DOWN) == target


def number_check(claim: str, passage: str) -> bool:
    """주장의 모든 숫자가 문단에 같은 값(반올림·버림 일치)으로 있으면 True."""
    wanted = parse(claim)
    if not wanted:
        return True
    have = passage_numbers(passage)
    return all(any(_same(h, w) for h in have) for w in wanted)
