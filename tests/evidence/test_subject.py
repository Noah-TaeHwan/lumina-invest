# tests/evidence/test_subject.py
"""주체 확인(A-3 spec 3절): 주장의 회사·부문·제품 이름 후보가 근거 문단에 경계 있게 있어야 ✅.

제품 실행기(runner)의 실험 정책 a3-subject-exp와 A-3 평가(lab/evidence/a3)가 같은 함수(subject 모듈)를 쓴다.
교체 예문은 A-2 조정 세트 통제 주장(탐색 허용 데이터)의 형태를 본뜬 합성 문장이다. 확인 세트 데이터는 쓰지 않았다.
"""
import itertools

import pytest

from app.services.evidence import subject as sj
from app.services.evidence.judge import Judgement, sys_decision

CO = "대원산업"


def groups(claim, company=CO):
    return [set(g) for g in sj.subject_groups(claim, company)]


# --- 후보 추출: 교체에 쓰인 이름이 후보에 들어가야 한다 ----------------------------------------------------------
@pytest.mark.parametrize("claim,name", [
    ("고려제강은 2006년 품질경영시스템 인증을 획득했다.", "고려제강"),            # 다른 회사(주어)
    ("연결회사의 제품 매출은 르노코리아(주)와 그 특수관계자 쪽에 몰려 있다.", "르노코리아"),  # 거래상대(법인 표지)
    ("순환기용제 대표 품목으로 아토젠정과 카덴자정이 꼽힌다.", "아토젠정"),          # 제품(나열)
    ("클라우드형 ERP로 CloudNova를 공급한다.", "CloudNova"),                    # 라틴 제품명
    ("건설 부문은 커피와 커피머신을 판매한다.", "건설"),                         # 일반 머리명사 앞 수식어
    ("2025년 냉연강판 설비는 평균 77.4%의 가동률을 기록했다.", "냉연강판"),
    ("2025년 식품포장소재 부문은 점착제를 원재료로 매입했다.", "식품포장소재"),
    ("배우 정해인과 로운은 큐브엔터테인먼트 소속 연예인이다.", "큐브엔터테인먼트"),   # '소속' 앞
    ("한빛소재 주식회사와 공급 계약을 맺었다.", "한빛소재"),                      # 띄어 쓴 법인 표지 앞
    ("한신공영 주식은 1977년부터 유가증권시장에서 거래되었다.", "한신공영"),        # '주식'·'지분' 앞
    ("최대주주는 두리홀딩스 지분 40%를 보유한다.", "두리홀딩스"),
])
def test_swap_names_are_candidates(claim, name):
    assert any(name in g for g in groups(claim)), groups(claim)


@pytest.mark.parametrize("claim", [
    "이 부문에서도 원재료가 따로 필요하지 않다.",       # 조사 두 겹(부문+에서+도)도 일반명사로 본다
    "회사는 보고서 제출일 기준으로 파생상품 거래를 하고 있지 않다.",
    "대원산업은 2006년에 설립되었다.",               # 선택 회사(자기 자신)는 후보가 아니다
    "㈜대원산업의 매출은 늘었다.",
    "주식회사 대원산업의 매출은 늘었다.",
    "대원산업 연결회사의 매출은 늘었다.",
])
def test_generic_and_self_are_not_candidates(claim):
    assert groups(claim) == []


def test_self_names_are_exact_or_dart_aliases():
    """자기 회사는 정규화 후 정확히 같은 이름만: DART 회사명·종목명(stock_name)·영문명(corp_name_eng).
    접두만 같은 이름은 자기 회사가 아니다(계열사 교체를 놓치지 않게, 리뷰 반영)."""
    hmc = ("현대자동차", "현대차", "Hyundai Motor Company")
    assert groups("현대차는 2006년 설립되었다.", hmc) == []
    assert groups("현대자동차는 2006년 설립되었다.", hmc) == []
    assert groups("Hyundai Motor Company는 2006년 설립되었다.", hmc) == []
    assert groups("포스코인터는 2006년 설립되었다.", ("포스코인터내셔널", "포스코인터")) == []


@pytest.mark.parametrize("claim,company,name", [
    ("포스코인터내셔널은 철강을 판다.", "포스코", "포스코인터내셔널"),       # 계열사 교체는 자기 회사가 아니다
    ("현대건설기계는 굴착기를 만든다.", "현대건설", "현대건설기계"),
    ("삼성전자서비스는 수리를 맡는다.", "삼성전자", "삼성전자서비스"),
    ("포스코는 철강을 판다.", "포스코인터내셔널", "포스코"),
])
def test_affiliate_is_not_self(claim, company, name):
    assert any(name in g for g in groups(claim, company)), groups(claim, company)


@pytest.mark.parametrize("claim,company", [
    ("네이버는 검색 광고를 판다.", ("NAVER", "NAVER", "NAVER Corp.")),          # 음역: DART에 근거 없음
    ("SM엔터테인먼트는 음원을 판다.", ("에스엠", "에스엠", "SM Entertainment Co., Ltd.")),  # 혼합 표기
    ("에프엔씨엔터테인먼트는 2006년 설립되었다.", "에프엔씨엔터"),               # DART 약식명보다 긴 본명
])
def test_self_alias_known_limits(claim, company):
    """근거 있는 별칭 소스(DART 회사명·종목명·영문명)에 없는 자기 회사 표기는 후보로 남는다(알려진 한계, c3·감사로 잰다)."""
    assert groups(claim, company) != []


@pytest.mark.parametrize("claim", [
    "회사는 생산능력을 두 배로 늘린다.",
    "수출 비중은 40%에 이른다.",
    "회사는 판매 가격을 올렸다.",
    "전방 수요가 높아진다.",
    "회사는 자동차 시트를 만든다.",
    "회사는 설비 투자를 늘리는 중이다.",
    "원가 절감과 공정 개선으로 성과를 냈다.",
    "배터리와 음반, 철광석을 다룬다.",
])
def test_predicates_and_common_nouns_are_not_candidates(claim):
    """서술형 활용(받침 있는 어간 + '다', 짧은 동사 어간 + '는')과 흔한 일반명사는 이름이 아니다(리뷰 반영)."""
    bad = {"늘린", "이른", "올렸", "높아진", "만든", "늘리", "절감", "개선", "성과", "배터리", "음반", "철광석"}
    got = {n for g in groups(claim, "케이산업") for n in g}
    assert not (bad & got), got


@pytest.mark.parametrize("claim,name", [
    ("주요 매출처는 삼성전자다.", "삼성전자"),        # 받침 없는 명사 + 서술격 '다'는 이름
    ("주요 고객은 현대모비스이다.", "현대모비스"),
    ("주요 고객은 코오롱인더스트리는 아니다.", "코오롱인더스트리"),  # 긴 이름 + '는'은 동사로 보지 않는다
])
def test_copula_names_are_kept(claim, name):
    assert any(name in g for g in groups(claim, "케이산업"))


@pytest.mark.parametrize("claim,passage,ok", [
    ("건설 부문은 아파트를 짓는다.", "건설사업부문은 아파트를 시공합니다.", True),    # 부문명 변형
    ("건설사업부문은 아파트를 짓는다.", "건설 부문은 아파트를 시공합니다.", True),
    ("선재사업부문은 서울사무소에서 판다.", "선재사업부는 서울사무소에서 판매합니다.", True),
    ("단조사업부문은 서울사무소에서 판다.", "선재사업부문은 서울사무소에서 판매합니다.", False),
])
def test_division_name_variants(claim, passage, ok):
    assert sj.subject_ok(claim, passage, "케이산업") is ok


# --- 교체 문장은 주체 확인에서 떨어진다 --------------------------------------------------------------------------
@pytest.mark.parametrize("claim,passage", [
    ("고려제강은 2006년 품질경영시스템 인증을 획득했다.", "당사는 2006년 국제 자동차분야 품질경영시스템 인증을 획득하였습니다."),
    ("건설 부문은 커피와 커피머신을 판매한다.", "커피 부문은 커피와 커피머신 등 상품을 판매하는 사업을 영위합니다."),
    ("2025년 냉연강판 설비는 평균 77.4%의 가동률을 기록했다.", "2025년 후판 설비의 평균 가동률은 77.4%입니다."),
    ("배우 정해인과 로운은 큐브엔터테인먼트 소속 연예인이다.", "당사 소속 연예인으로 정해인, 로운 등이 있습니다."),
    ("삼성과 공급 계약을 맺었다.", "삼성전자와 공급 계약을 체결하였습니다."),          # 경계: '삼성' ≠ '삼성전자'
])
def test_swapped_subject_fails(claim, passage):
    assert not sj.subject_ok(claim, passage, CO)


@pytest.mark.parametrize("claim,passage", [
    ("커피 부문은 커피와 커피머신을 판매한다.", "커피 부문은 커피와 커피머신 등 상품을 판매하는 사업을 영위합니다."),
    ("대원산업은 2006년 품질경영시스템 인증을 획득했다.", "당사는 2006년 국제 자동차분야 품질경영시스템 인증을 획득하였습니다."),
    ("이 부문에서도 원재료가 따로 필요하지 않다.", "MD 상품은 외주 생산으로 조달합니다."),   # 후보 없음 → 통과
])
def test_true_subject_passes(claim, passage):
    assert sj.subject_ok(claim, passage, CO)


# --- 이름 표기 변형: 같은 이름이면 통과해야 한다(오탐 방지) ------------------------------------------------------
@pytest.mark.parametrize("claim,passage", [
    ("한빛소재와 공급 계약을 맺었다.", "㈜한빛소재와 공급계약을 체결하였습니다."),            # ㈜
    ("㈜한빛소재와 공급 계약을 맺었다.", "한빛소재(주)와 공급계약을 체결하였습니다."),         # ㈜ ↔ (주)
    ("한빛소재 주식회사와 공급 계약을 맺었다.", "주식회사 한빛소재와 공급계약을 체결하였습니다."),  # 주식회사 앞뒤
    ("(주)한빛소재는 부품을 공급한다.", "한빛소재 주식회사는 부품을 공급합니다."),
    ("Hanwha Ocean과 공급 계약을 맺었다.", "Hanwha Ocean Co., Ltd.와 공급계약을 체결하였습니다."),  # 영문 법인 접미
    ("SYSTEMEVER를 공급한다.", "클라우드 ERP SystemEver를 공급합니다."),                    # 라틴 대소문자
    ("고려제강과 거래한다.", "주요 매입처는 고려 제강입니다."),                              # 문단 쪽 띄어쓰기
    ("고려 제강과 거래한다.", "주요 매입처는 고려제강입니다."),                              # 주장 쪽 띄어쓰기
    ("단조사업부문은 영업소로 지역을 나누어 판매한다.", "단조사업 부문은 서울사무소와 영업소로 판매합니다."),
    ("Incheon 공장에서 생산한다.", "Incheon 공장에서 생산합니다."),                        # 'Inc'를 법인 접미로 지우지 않는다
])
def test_notation_variants_pass(claim, passage):
    assert sj.subject_ok(claim, passage, CO)


def test_abbreviation_defined_in_passages_passes():
    """문단 묶음 안 괄호 정의(이하 'FNC')로 약칭과 전체 이름을 같은 것으로 본다. 정의가 다른 문단에 있어도 된다."""
    passages = ["에프엔씨엔터테인먼트(이하 'FNC')는 2006년 설립되었습니다.",
                "에프엔씨엔터테인먼트 소속 배우로 정해인이 있습니다.",
                "주요 원재료는 없습니다."]
    assert sj.subject_valid("FNC 소속 배우로 정해인이 있다.", passages, "케이스튜디오") == [False, True, False]  # 0번에는 정해인이 없다
    assert sj.subject_valid("에프엔씨엔터테인먼트는 2006년 설립되었다.", ["FNC(에프엔씨엔터테인먼트)는 상장사다.", "FNC는 2006년 설립"],
                            "케이스튜디오") == [True, True]


def test_undefined_abbreviation_is_known_false_negative():
    """문단에 정의가 없는 약칭·음역(SK ↔ 에스케이)은 못 맞춘다. A-3 c3(표기 변형 참) 주장으로 이 오탐을 잰다."""
    assert not sj.subject_ok("SK하이닉스에 메모리를 납품한다.", "에스케이하이닉스에 메모리를 납품합니다.", CO)


def test_missing_subjects_lists_failed_groups():
    claim = "고려제강은 냉연강판 설비를 가동한다."
    passage = "고려제강은 후판 설비를 가동합니다."
    assert sj.missing_subjects(claim, passage, CO) == ["냉연강판"]


# --- 판정: a2-v1 위에 '지지됨'만 좁히는 순수 제한 -----------------------------------------------------------------
def test_decision_matches_sys_when_all_subjects_ok():
    j = Judgement([0.9, 0.2], [0.0, 0.1], True, 1)
    assert sj.subject_decision(j, [True, True], [True, True], 0.85, 0.35) == sys_decision(j, [True, True], 0.85, 0.35)


def test_decision_moves_source_to_passage_with_subject():
    j = Judgement([0.95, 0.9], [0.0, 0.0], True, 1)
    assert sj.subject_decision(j, [True, True], [False, True], 0.85, 0.35) == ("supported", 1, 0.9)


def test_subject_failure_does_not_create_contradiction():
    """숫자만 본 S_V로 반박 규칙을 적용한다(주체 확인으로 S_V를 낮춰 ⚠️가 늘지 않게)."""
    j = Judgement([0.9, 0.1], [0.0, 0.5], True, 1)
    assert sys_decision(j, [True, True], 0.85, 0.35)[0] == "supported"
    assert sys_decision(j, [False, True], 0.85, 0.35)[0] == "contradicted"  # 순진하게 valid에 합치면 생기는 일
    assert sj.subject_decision(j, [True, True], [False, True], 0.85, 0.35) == ("no_evidence", None, 0.1)


def test_decision_is_pure_restriction():
    """모든 조합에서 ✅ 집합 ⊆ a2-v1 ✅, ⚠️·미판정은 a2-v1과 같다."""
    vals = [0.0, 0.4, 0.86, 0.95]
    for s1, s2, c1, c2 in itertools.product(vals, vals, [0.0, 0.36, 0.9], [0.0, 0.5]):
        j = Judgement([s1, s2], [c1, c2], True, 1)
        for valid in itertools.product([True, False], repeat=2):
            for subj in itertools.product([True, False], repeat=2):
                base = sys_decision(j, list(valid), 0.85, 0.35)
                got = sj.subject_decision(j, list(valid), list(subj), 0.85, 0.35)
                if got[0] == "supported":
                    assert base[0] == "supported"
                assert (got[0] == "contradicted") == (base[0] == "contradicted")
    assert sj.subject_decision(Judgement([0.0], [0.0], False, 1), [True], [True], 0.85, 0.35)[0] == "unjudged"


def test_high_ok_requires_subject_on_best_passage():
    assert sj.subject_high_ok(True, 1, [False, True])
    assert not sj.subject_high_ok(True, 0, [False, True])
    assert not sj.subject_high_ok(False, 1, [True, True])
    assert not sj.subject_high_ok(True, None, [True])
    assert not sj.subject_high_ok(True, 0, [])  # 주체 확인 결과가 없으면 상단 구간이 아니다(IndexError 아님)
