# tests/evidence/test_name_condition.py
"""1차 필터 상단 구간의 회사명 조건(A-2 spec 6.1·6.3절): 주장의 회사명 후보가 근거 문단에 그대로 있어야 한다.

제품 실행기(runner)와 A-2 평가(lab/evidence/a2)가 같은 함수(lexical.lex_features·tier_route)를 쓰는지도 본다.
"""
import inspect

import pytest

from app.services.evidence import lexical
from app.services.evidence import runner as rn

CO = "케이씨건설"


@pytest.mark.parametrize("claim,expected", [
    ("태영건설은 물류센터 공사를 수행한다.", {"태영건설"}),            # 문장 첫 주어
    ("기초자산을 발행한 포스코는 같은 그룹 소속이다.", {"포스코"}),      # 문장 중간 주제어
    ("SDI-Ford Synergy 법인을 연결에 넣었다.", {"SDI-Ford", "Synergy"}),  # 라틴 대문자 토큰
    ("브랜드 '링구아랩'은 점유율 1위다.", {"링구아랩"}),                # 따옴표 안 이름
    ("케이씨건설의 매출은 늘었다.", {"케이씨건설", "매출"}),             # 선택 회사명 + 주제어
    ("㈜한빛소재와 거래한다.", {"한빛소재"}),                          # 법인 표지
    ("(주)한빛소재는 부품을 만든다.", {"한빛소재"}),
    ("메모리를 생산하는 곳이 많다.", set()),                           # 서술형(하는)·1자 어간은 후보 아님
])
def test_name_candidates(claim, expected):
    assert lexical.name_candidates(claim, CO) == expected


def test_names_in_passage_requires_every_candidate_verbatim():
    passage = "케이씨건설은 진천 물류센터 공사를 민간 공사로 수행하고 있다."
    assert lexical.names_in_passage("케이씨건설은 물류센터 공사를 수행한다.", passage, CO)
    assert not lexical.names_in_passage("태영건설은 물류센터 공사를 수행한다.", passage, CO)
    assert lexical.names_in_passage("물류센터 공사를 수행한다.", passage, CO)  # 후보 없음 → 통과


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


def test_runner_uses_shared_tier_functions():
    src = inspect.getsource(rn)
    assert "lex_features(" in src and "tier_route(" in src
    assert "company in passages" not in src
