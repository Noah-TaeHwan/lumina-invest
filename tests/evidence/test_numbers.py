# tests/evidence/test_numbers.py
from decimal import Decimal

from app.services.evidence import numbers as nb


def test_parse_compound_units_and_kinds():
    assert nb.parse("1조 2,345억 원") == [nb.Num(Decimal("1234500000000"), Decimal("100000000"), "abs")]
    assert nb.parse("33.0%") == [nb.Num(Decimal("33.0"), Decimal("0.1"), "pct")]
    assert nb.parse("3.5%p") == [nb.Num(Decimal("3.5"), Decimal("0.1"), "pctp")]
    assert nb.parse("(1,234) 감소")[0].value == Decimal("-1234")


def test_number_check_units_and_truncation():
    row = "[가. 매출 표, 단위 백만원] 구분: 매출 | 2025: 1,006,771"
    assert nb.number_check("매출은 약 1조 67억원이다.", row)
    assert not nb.number_check("매출은 2조원이다.", row)
    assert nb.number_check("매출은 1.23조원이다.", "매출 12,345억원")
    assert nb.number_check("숫자가 없는 주장이다.", row)
    assert not nb.number_check("2025년에 설립했다.", "2024년 설립")


def test_percent_table_unit():
    row = "[나. 비중 표, 단위 %] 구분: DX | 비중: 33.04"
    assert nb.number_check("DX 비중은 33.0%다.", row)
    assert not nb.number_check("DX 비중은 34%다.", row)
    assert nb.number_check("비중은 33.0%다.", "비중 33.04%")


def test_segments_use_their_own_units():
    text = "[가 표, 단위 억원] 매출: 120 [나 표, 단위 백만원] 이익: 3,000"
    assert nb.number_check("매출 120억원, 이익 30억원", text)
