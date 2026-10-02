# tests/evidence/test_baselines.py
from lab.evidence import baselines as bl


def test_lex_score_uses_number_check_and_bigrams():
    p_ok, p_bad = "삼성전자 DX 매출 비중 33.0% 기록", "삼성전자 DX 매출 비중 40% 기록"
    assert bl.lex_score("DX 매출 비중 33.0%", [p_bad]) == 0.0
    assert bl.lex_score("DX 매출 비중 33.0%", [p_bad, p_ok]) == 1.0
    assert 0 < bl.lex_score("반도체를 만든다", ["반도체 생산"]) < 1


def test_emb_score_and_llm_parse():
    assert abs(bl.emb_score([1, 0], [[0, 1], [1, 0]]) - 1.0) < 1e-9
    assert bl.parse_llm_score("점수: 85") == 0.85
    assert bl.parse_llm_score("150") is None and bl.parse_llm_score("모름") is None
    m = bl.llm_messages("주장", ["가", "나"])
    assert "[문단 2] 나" in m[1]["content"] and "[주장] 주장" in m[1]["content"]
