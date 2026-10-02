"""주장의 숫자가 문단에 같은 값으로 있는지 코드로 확인한다(spec 3절, 필요조건).

같은 값이란 문단 값을 주장 표기의 마지막 자리 단위(step)로 반올림하거나 버림했을 때 주장 값과 같아지는 것이다
(예: 1조 67억 ← 1,006,771백만은 버림으로 일치, 2조 ← 1,006,771백만은 불일치). 부호와 기간·지표가 맞는지는
판단하지 않는다(JEV 몫). 실제 공시 표기에 맞춰 문단 숫자는 여러 해석을 함께 둔다:
표 단위를 곱한 값과 곱하지 않은 값(연도·복사 셀), 표에 %가 보이면 퍼센트 해석.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

_UNIT = {"조": 10**12, "십억": 10**9, "억": 10**8, "천만": 10**7, "백만": 10**6, "만": 10**4, "천": 10**3}
_TOKEN = re.compile(r"(\()?(\d[\d,]*(?:\.\d+)?)\s*(조|십억|억|천만|백만|만|천)?\s*(%p|%포인트|%|퍼센트)?")
_SEGMENT = re.compile(r"(?=\[[^\]]*표[^\]]*\])")
_TABLE_HEAD = re.compile(r"\[[^\]]*표[^\]]*\]")
_TABLE_UNIT = re.compile(r"단위 ([^\]]+)\]")
_DATE3 = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")
_DATE2 = re.compile(r"(\d{4})\.(\d{1,2})(?![\d.])")
_CHEON = re.compile(r"(\d+)천\s*(\d{1,3})?\s*(만|억|조)")
_MIN_DIGITS = 4


@dataclass(frozen=True)
class Num:
    """정규화한 숫자: 값, 허용 단위(마지막 자리), 종류(abs·pct·pctp), 표기 숫자열(단위 없는 자릿수)."""

    value: Decimal
    step: Decimal
    kind: str
    digits: str = field(default="", compare=False)


def _normalize(text: str) -> str:
    """점 날짜(2025.12.31)를 연·월·일로, '3천500만'을 '3500만'으로 바꾼다."""
    text = _DATE3.sub(r"\1년 \2월 \3일", text)
    text = _DATE2.sub(r"\1년 \2월", text)
    return _CHEON.sub(lambda m: f"{int(m.group(1)) * 1000 + int(m.group(2) or 0)}{m.group(3)}", text)


def _scale(unit_text: str) -> int:
    """표 단위 문자열의 금액 배수(예: '백만원' → 10^6). 알 수 없으면 1."""
    u = unit_text.strip()
    for k, v in _UNIT.items():
        if u.startswith(k):
            return v
    return 1


def parse(text: str, default_scale: int = 1, default_kind: str = "abs") -> list[Num]:
    """숫자 표현을 차례로 뽑는다. '1조 2,345억'처럼 큰 단위 뒤 작은 단위는 하나로 합친다."""
    text = _normalize(text)
    out: list[Num] = []
    prev_mult, prev_end = 0, -1
    for m in _TOKEN.finditer(text):
        paren, digits, unit, pct = m.groups()
        raw = digits.replace(",", "").rstrip(".")
        if not raw or not raw.replace(".", "", 1).isdigit():
            continue
        val = Decimal(raw)
        dec = -int(val.as_tuple().exponent) if "." in raw else 0
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
            raw = ""
        out.append(Num(v, step, kind, raw))
        prev_mult, prev_end = (_UNIT[unit] if unit else 0), m.end()
    return out


def passage_numbers(text: str) -> list[Num]:
    """문단을 표 행 접두어 단위로 나눠 숫자를 뽑는다. 표 조각의 맨 숫자는 여러 해석을 함께 둔다."""
    nums: list[Num] = []
    for seg in _SEGMENT.split(text):
        head = _TABLE_HEAD.match(seg)
        if not head:
            nums += parse(seg)
            continue
        um = _TABLE_UNIT.search(head.group(0))
        scale = _scale(um.group(1)) if um else 1
        body = seg[head.end():]
        nums += parse(body, scale, "abs")
        if scale != 1:
            nums += parse(body, 1, "abs")
        if "%" in seg:
            nums += parse(body, 1, "pct")
    return nums


def _same(h: Num, w: Num) -> bool:
    """문단 값 h를 주장 w의 마지막 자리 단위로 반올림 또는 버림했을 때 w와 같은가(부호 무시).

    주장에 단위가 붙고(예: 27,102백만원) 문단에 같은 자릿수의 맨 숫자만 있으면(표 단위 누락) 4자리 이상일 때 같다고 본다.
    """
    if h.kind != w.kind:
        return False
    q, target = abs(h.value) / w.step, abs(w.value) / w.step
    if q.to_integral_value(ROUND_HALF_UP) == target or q.to_integral_value(ROUND_DOWN) == target:
        return True
    return bool(w.digits) and w.digits == h.digits and len(w.digits.replace(".", "")) >= _MIN_DIGITS


def number_check(claim: str, passage: str) -> bool:
    """주장의 모든 숫자가 문단에 같은 값(반올림·버림 일치)으로 있으면 True."""
    wanted = parse(claim)
    if not wanted:
        return True
    have = passage_numbers(passage)
    return all(any(_same(h, w) for h in have) for w in wanted)
