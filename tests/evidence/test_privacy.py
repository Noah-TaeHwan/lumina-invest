# tests/evidence/test_privacy.py
import pytest

from app.services.evidence import privacy


@pytest.mark.parametrize("text", [
    "문의는 hong.gildong@example.co.kr 로 주세요.",
    "담당자 연락처는 010-1234-5678입니다.",
    "담당자 연락처는 01012345678입니다.",
    "휴대폰 011 234 5678로 연락",
    "주민등록번호 900101-1234567이 적혀 있다.",
])
def test_pii_detected(text):
    assert privacy.has_pii(text)


@pytest.mark.parametrize("text", [
    "회사 대표번호는 02-2255-0114이다.",
    "매출액은 174,887,683백만원이다.",
    "접수번호 20260312000123 사업보고서",
    "2025.12.31 기준 영업이익은 32조원이다.",
    "DX 부문 비중은 61.2%다.",
    "",
])
def test_pii_not_detected(text):
    assert not privacy.has_pii(text)
