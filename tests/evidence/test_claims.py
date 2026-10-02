# tests/evidence/test_claims.py
from app.services.evidence import claims


def test_split_sentences_handles_bullets_and_newlines():
    text = "삼성전자는 반도체를 만든다. 매출은 300조다!\n- 주요 제품은 DRAM이다\n1) 네."
    assert claims.split_sentences(text) == ["삼성전자는 반도체를 만든다.", "매출은 300조다!", "주요 제품은 DRAM이다"]


def test_numeric_tokens_strip_commas():
    assert claims.numeric_tokens("매출 1,006,771억원, 비중 33.0%, 2025년") == {"1006771", "33.0", "2025"}


def test_new_values_absent():
    passages = ["DX 부문 매출액: 1,006,771", "2025년 사업"]
    assert claims.new_values_absent("DX 매출은 2,000,000억원", "DX 매출은 1,006,771억원", passages)
    assert not claims.new_values_absent("2025년 매출", "2024년 매출", passages)
    assert not claims.new_values_absent("LG화학이 만든다", "삼성전자가 만든다", ["LG화학 공급"], ("LG화학",))
    assert claims.new_values_absent("A는 만들지 않는다", "A는 만든다", passages)
