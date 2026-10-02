# app/services/evidence/privacy.py
"""외부 판정 전송 전 개인정보 패턴 검사(A-2 spec 8절). 걸리면 판정 실행 전체를 skipped(pii)로 끝낸다.

공시 문단에 흔한 유선 대표번호·금액·접수번호는 잡지 않도록 휴대전화와 하이픈 있는 주민등록번호만 본다.
"""
from __future__ import annotations

import re

PII_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "mobile": re.compile(r"(?<!\d)01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)"),
    "rrn": re.compile(r"(?<!\d)\d{6}\s?-\s?[1-4]\d{6}(?!\d)"),
}


def has_pii(text: str) -> bool:
    """이메일, 휴대전화 번호, 주민등록번호 형태가 하나라도 있으면 True."""
    return any(p.search(text) for p in PII_PATTERNS.values())
