# tests/evidence/test_name_condition.py
"""1차 필터 상단 구간의 회사명 조건(A-2 spec 6.1·6.3절): 주장의 회사명 후보가 근거 문단에 그대로 있어야 한다.

제품 실행기(runner)와 A-2 평가(lab/evidence/a2)는 같은 함수(lexical.lex_features·tier_route)를 쓴다.
"""
import pytest

from app.services.evidence import lexical

CO = "케이씨건설"


@pytest.mark.parametrize("claim,must,must_not", [
    ("태영건설은 물류센터 공사를 수행한다.", {"태영건설"}, set()),             # 문장 첫 주어
    ("기초자산을 발행한 포스코는 같은 그룹 소속이다.", {"포스코"}, {"그룹"}),   # 문장 중간 주제어
    ("SDI-Ford Synergy 법인을 연결에 넣었다.", {"SDI-Ford", "Synergy"}, set()),  # 라틴 대문자 토큰
    ("브랜드 '링구아랩'은 점유율 1위다.", {"링구아랩"}, {"1위"}),             # 따옴표 안 이름
    ("케이씨건설의 매출은 늘었다.", {"케이씨건설"}, {"매출"}),                # 선택 회사명, 일반명사 제외
    ("㈜한빛소재와 거래한다.", {"한빛소재"}, set()),                         # 법인 표지
    ("(주)한빛소재는 부품을 만든다.", {"한빛소재"}, set()),
    # 리뷰 지적: 주어 자리가 아닌 회사명
    ("당사는 현대자동차에 배터리를 공급한다.", {"현대자동차"}, {"당사"}),
    ("주요 매출처는 삼성전자이다.", {"삼성전자"}, {"매출처", "주요"}),
    ("셀트리온과 공동 개발 계약을 체결했다.", {"셀트리온"}, {"계약", "체결했"}),
    ("메모리를 생산하는 곳이 많다.", set(), {"생산하", "곳", "많"}),           # 서술형·1자 어간은 후보 아님
])
def test_name_candidates(claim, must, must_not):
    got = lexical.name_candidates(claim, CO)
    assert must <= got and not (must_not & got), got


def test_generic_nouns_are_excluded():
    assert {"당사", "회사", "매출", "매출처", "제품", "사업"} <= set(lexical.GENERIC_NOUNS)


def test_names_in_passage_requires_every_candidate_verbatim():
    passage = "케이씨건설은 진천 물류센터 공사를 민간 공사로 수행하고 있다."
    assert lexical.names_in_passage("케이씨건설은 물류센터 공사를 수행한다.", passage, CO)
    assert not lexical.names_in_passage("태영건설은 물류센터 공사를 수행한다.", passage, CO)
    assert lexical.names_in_passage("물류센터 공사를 수행한다.", passage, CO)  # 후보 없음 → 통과


def test_names_match_on_token_boundary_not_substring():
    assert not lexical.names_in_passage("삼성과 거래한다.", "주요 고객은 삼성전자와 애플이다.", CO)
    assert lexical.names_in_passage("삼성전자와 거래한다.", "주요 고객은 삼성전자와 애플이다.", CO)
    assert lexical.names_in_passage("삼성전자와 거래한다.", "고객: 삼성전자(주), 애플", CO)
    assert not lexical.names_in_passage("LG와 거래한다.", "고객은 LG전자다.", CO)
    assert lexical.names_in_passage("당사는 현대자동차에 공급한다.", "당사는 현대자동차에게 배터리를 판다.", CO)


def test_lex_features_and_tier_route():
    ps = ["회사는 메모리 반도체와 스마트폰을 생산한다.", "LG전자는 가전을 판매한다."]
    f = lexical.lex_features("회사는 메모리 반도체와 스마트폰을 생산한다.", ps, CO)
    assert (f.lex, f.best, f.high_ok) == (1.0, 0, True)
    swap = lexical.lex_features("LG전자는 메모리 반도체와 스마트폰을 생산한다.", ps, CO)
    assert swap.best == 0 and swap.high_ok is False  # 근거 문단에 LG전자가 없다
    assert lexical.tier_route(f.lex, f.high_ok, None, None) == "jev"
    assert lexical.tier_route(f.lex, f.high_ok, None, 0.8) == "lex_high"
    assert lexical.tier_route(swap.lex, swap.high_ok, None, 0.8) == "jev"
    assert lexical.tier_route(0.1, True, 0.2, 0.8) == "lex_low"
    assert lexical.tier_route(0.2, True, 0.2, None) == "jev"           # 하단은 lex < θ_low
    assert lexical.tier_route(0.8, True, None, 0.8) == "lex_high"      # 상단은 lex ≥ θ_high
    assert lexical.tier_route(0.5, True, 0.6, 0.4) == "lex_low"        # 겹치면 하단이 먼저
    zero = lexical.lex_features("2030년 매출은 9조원이다.", ps, CO)
    assert (zero.lex, zero.best, zero.high_ok) == (0.0, None, False)
