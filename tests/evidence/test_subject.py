# tests/evidence/test_subject.py
"""주체 확인(A-3 spec 3절): 주장의 회사·부문·제품 이름 후보가 근거 문단에 경계 있게 있어야 ✅.

제품 실행기(runner)의 실험 정책 a3-subject-exp와 A-3 평가(lab/evidence/a3)가 같은 함수(subject 모듈)를 쓴다.
교체 예문은 A-2 조정 세트 통제 주장(탐색 허용 데이터)의 형태를 본뜬 합성 문장이다. 확인 세트 데이터는 쓰지 않았다.
후보는 허용 목록(엔티티)만이다: 법인 표지 토큰, 라틴 대문자 약칭, 따옴표 안 이름, 상장사 이름 사전, 부문 이름.
"""
import itertools

import pytest

from app.services.evidence import subject as sj
from app.services.evidence.judge import Judgement, sys_decision

CO = "대원산업"
# 상장사 이름 사전 픽스처(런타임은 DART corpCode.xml의 상장사 corp_name·corp_eng_name). 이름은 예시용이다
NAMES = sj.CompanyNames(
    ["고려제강", "르노코리아", "한신공영", "큐브엔터", "기아", "대원산업", "현대모비스", "삼성전자", "포스코",
     "포스코인터내셔널", "현대건설", "현대건설기계", "삼성전자서비스", "코오롱인더스트리", "SK하이닉스", "대상", "삼성"],
    eng=["Hanwha Ocean", "Hyundai Motor Company"])


def groups(claim, company=CO, names=NAMES):
    return [set(g) for g in sj.subject_groups(claim, company, names=names)]


def ok(claim, passage, company=CO, names=NAMES):
    return sj.subject_ok(claim, passage, company, names=names)


# --- 허용 목록(엔티티)만 후보다(spec 3.2, 2026-10-04 탐색 결과로 바꿈) -----------------------------------------------
@pytest.mark.parametrize("claim,name", [
    ("고려제강은 2006년 품질경영시스템 인증을 획득했다.", "고려제강"),                 # (d) 상장사 이름 사전
    ("배우 정해인과 로운은 큐브엔터 소속 연예인이다.", "큐브엔터"),
    ("한신공영 주식은 1977년부터 유가증권시장에서 거래되었다.", "한신공영"),
    ("연결회사의 제품 매출은 르노코리아(주)와 그 특수관계자 쪽에 몰려 있다.", "르노코리아"),  # (a) 법인 표지
    ("㈜한빛소재와 공급 계약을 맺었다.", "한빛소재"),
    ("한빛소재 주식회사와 공급 계약을 맺었다.", "한빛소재"),                          # 띄어 쓴 법인 표지 앞 토큰
    ("최대주주는 ㈜두리홀딩스 지분 40%를 보유한다.", "두리홀딩스"),
    ("클라우드형 ERP를 공급한다.", "ERP"),                                       # (b) 라틴 대문자 약칭
    ("ERP(Enterprise Resource Planning) 개발이 주력이다.", "ERP"),                # 괄호 앞 약칭
    ("브랜드 '링구아랩'은 점유율 1위다.", "링구아랩"),                               # (c) 따옴표
    ("건설 부문은 커피와 커피머신을 판매한다.", "건설"),                              # (e) 부문 이름(띄어 씀)
    ("2025년 식품포장소재 부문은 점착제를 원재료로 매입했다.", "식품포장소재"),
    ("단조사업부문은 서울에서 판매한다.", "단조사업부문"),                          # (e) 붙여 씀
    ("Hanwha Ocean과 공급 계약을 맺었다.", "Hanwha Ocean"),                       # (d) 영문 상장사 이름
])
def test_allowlisted_names_are_candidates(claim, name):
    assert any(name in g for g in groups(claim)), groups(claim)


@pytest.mark.parametrize("claim", [
    "순환기용제 대표 품목으로 아토젠정과 카덴자정이 꼽힌다.",      # 일반명사로 된 제품명
    "클라우드형 ERP로 CloudNova를 공급한다.",                  # 대문자 약칭이 아닌 라틴 제품명
    "2025년 냉연강판 설비는 평균 77.4%의 가동률을 기록했다.",     # 부문 꼬리가 아닌 설비 이름
    "교체용 피스톤링은 신조선용보다 비싸다.",
])
def test_non_entity_swaps_are_known_limits(claim):
    """허용 목록 밖(일반명사로 된 제품·설비 이름)의 교체는 놓친다(prereg known_limits, H-swap 하위 유형별 보고)."""
    got = {n for g in groups(claim) for n in g}
    assert not got & {"아토젠정", "카덴자정", "CloudNova", "냉연강판", "피스톤링"}, got


@pytest.mark.parametrize("claim", [
    "이 부문에서도 원재료가 따로 필요하지 않다.",
    "회사는 보고서 제출일 기준으로 파생상품 거래를 하고 있지 않다.",
    "대원산업은 2006년에 설립되었다.",               # 선택 회사(자기 자신)는 후보가 아니다
    "㈜대원산업의 매출은 늘었다.",
    "주식회사 대원산업의 매출은 늘었다.",
    "대원산업 연결회사의 매출은 늘었다.",
    "회사는 생산능력을 두 배로 늘린다.",              # 서술어·일반명사는 허용 목록이 아니다
    "원가 절감과 공정 개선으로 성과를 냈다.",
    "고객을 대상으로 판매한다.",                      # 일상어와 겹치는 상장사 이름(대상)은 빼 둔다
    "진흥기업의 영업부문은 크게 토목, 건축/주택, 플랜트의 3개 분야로 나누어집니다.",  # 일반 부문어(영업부문)
])
def test_generic_self_and_predicates_are_not_candidates(claim):
    assert groups(claim) == []


# A-2 조정 세트 a3-explore(로컬, 2026-10-04)에서 a2-v1 정답 ✅를 잃은 12건(문장: lab/evidence/study_a2/claims.jsonl).
# 잃게 만든 후보(missing)는 모두 보통명사였다. 허용 목록에서는 그 낱말이 후보가 되지 않는다.
LOST_12 = [
    ("00111874-q2-n1", "대원산업", "대원산업이 국내에서 시트를 제작해 공급하는 주요 차종은 카니발, 니로, 스토닉, 모닝 등 기아자동차의 차종입니다.", {"차종"}),
    ("00127875-q4-n1", "삼익제약", "삼익제약 인천공장의 정제 생산설비 가동률은 최근 3년간 20.86%에서 21.31%로 증가한 후 31.05%로 증가했습니다.", {"인천공장"}),
    ("00133991-q6-n3", "세아특수강", "또한, 세아특수강은 글로벌 품질경영체제를 구축하기 위해 고객의 다양한 요구를 내부 프로세스에 반영하고, 품질 경영시스템 정착, 개선 전문가 집중 육성, 교육을 통한 전 직원의 업무능력 향상 등의 노력을 통해 글로벌 리더로서의 위상에 맞는 품질과 경쟁력을 확보하고 있습니다.", {"노력"}),
    ("00150828-q2-n1", "진흥기업", "진흥기업의 영업부문은 크게 토목, 건축/주택, 플랜트의 3개 분야로 나누어집니다.", {"영업부문"}),
    ("00150828-q2-n2", "진흥기업", "토목 부문은 SOC 관련 공사들을 주로 수행하고 있습니다.", {"공사들"}),
    ("00150828-q2-n4", "진흥기업", "플랜트 부문은 업무/상업 시설, 토목/환경, SOC 사업 등 다양한 건설 사업분야에 참여하고 있습니다.", set()),  # 이 문장의 missing은 SOC(아래 남는 후보)
    ("00152437-q2-n1", "케이프", "케이프가 생산하는 실린더라이너는 신조용과 교체용으로 나뉘며, 신조용은 엔진생산업체, 엔진메이커 등 기존 고객에게 공급되고, 교체용은 교체용 수요에 대응하기 위해 생산공정을 구축하여 국내 및 해외의 교체용 제품으로 공급됩니다.", {"신조용"}),
    ("00205003-q2-n1", "좋은사람들", "좋은사람들이 운영하는 주요 내의류 브랜드로는 보디가드, 섹시쿠키, 예스, 제임스딘, ESCADA, hoopoe, 돈앤돈스, 퍼스트올로 등이 있습니다.", {"내의류"}),
    ("00205003-q5-n1", "좋은사람들", "보고기간종료일 현재 연결회사가 노출된 환위험의 주요 통화는 USD, EUR, CNY입니다.", {"환위험", "통화"}),
    ("00220622-q1-n1", "서호전기", "서호전기는 항만크레인구동제어시스템과 인버터, 컨버터 등 구동제어기기 전반에 걸친 제품의 제조, 판매를 주력으로 영위하고 있습니다.", {"주력"}),
    ("00524786-q1-n1", "비엠티", "비엠티는 산업용 밸브와 정밀 피팅을 주력 제품으로 조선/해양플랜트, 발전(원자력), 플랜트산업, 반도체 생산업체에 공급하고 있습니다.", {"밸브", "피팅", "주력"}),
    ("00532129-q1-n1", "영림원소프트랩", "영림원소프트랩은 ERP(Enterprise Resource Planning) 개발 및 판매를 주력으로 하는 국내 대표적인 기업입니다.", {"주력"}),
]
# 허용 목록에서도 남는 후보(문장만으로 판단: 문단에 그대로 있는지는 로컬 재측정으로 확인한다).
# 진흥기업 두 문장의 SOC는 탐색에서 missing이었고 (b) 대문자 약칭이라 여전히 후보다 — 이 두 건은 계속 잃을 수 있다
LOST_12_REMAINING = {"00150828-q2-n2": {"토목", "SOC"}, "00150828-q2-n4": {"플랜트", "SOC"},
                     "00205003-q2-n1": {"ESCADA"}, "00205003-q5-n1": {"USD", "EUR", "CNY"},
                     "00532129-q1-n1": {"ERP"}}


def test_lost_12_claims_have_no_common_noun_candidates():
    for cid, co, text, missing in LOST_12:
        got = {g[0] for g in sj.subject_groups(text, co, names=NAMES)}
        assert not (got & missing), (cid, got)
        assert got == LOST_12_REMAINING.get(cid, set()), (cid, got)


def test_division_candidate_is_checked_only_against_passages_naming_divisions():
    """(e) 부문 이름은 같은 문단의 부문명과 대조한다: 문단에 부문 이름이 하나도 없으면 문제 삼지 않는다."""
    assert ok("토목 부문은 도로 공사를 수행한다.", "당사는 도로 공사를 수행하고 있습니다.")
    assert ok("토목 부문은 도로 공사를 수행한다.", "토목사업부문은 도로 공사를 수행합니다.")
    assert not ok("토목 부문은 도로 공사를 수행한다.", "건축사업부문은 도로 공사를 수행합니다.")


def test_self_names_are_exact_or_dart_aliases():
    """자기 회사는 정규화 후 정확히 같은 이름만: DART 회사명·종목명(stock_name)·영문명(corp_name_eng).
    접두만 같은 이름은 자기 회사가 아니다(계열사 교체를 놓치지 않게)."""
    hmc = ("현대자동차", "현대차", "Hyundai Motor Company")
    assert groups("현대차는 2006년 설립되었다.", hmc) == []
    assert groups("현대자동차는 2006년 설립되었다.", hmc) == []
    assert groups("Hyundai Motor Company는 2006년 설립되었다.", hmc) == []
    assert groups("포스코인터는 2006년 설립되었다.", ("포스코인터내셔널", "포스코인터")) == []


@pytest.mark.parametrize("claim,company,name", [
    ("포스코인터내셔널은 2006년 설립되었다.", "포스코", "포스코인터내셔널"),   # 계열사 교체는 자기 회사가 아니다
    ("현대건설기계는 2006년 설립되었다.", "현대건설", "현대건설기계"),
    ("삼성전자서비스는 2006년 설립되었다.", "삼성전자", "삼성전자서비스"),
    ("포스코는 2006년 설립되었다.", "포스코인터내셔널", "포스코"),
])
def test_affiliate_is_not_self(claim, company, name):
    assert any(name in g for g in groups(claim, company)), groups(claim, company)


def test_self_alias_known_limits():
    """DART 별칭에 없는 자기 회사 표기: 허용 목록 밖이면 후보가 아니라 해가 없고(네이버 ↔ NAVER),
    허용 목록 규칙(대문자 약칭)에 걸리면 후보로 남아 ❔가 될 수 있다(SM ↔ 에스엠, 알려진 한계)."""
    assert groups("네이버는 검색 광고를 판다.", ("NAVER", "NAVER", "NAVER Corp.")) == []
    assert groups("SM은 음원을 판다.", ("에스엠", "에스엠", "SM Entertainment Co., Ltd.")) == [{"SM"}]


@pytest.mark.parametrize("claim,name", [
    ("주요 매출처는 삼성전자다.", "삼성전자"),
    ("주요 고객은 현대모비스이다.", "현대모비스"),
    ("주요 고객은 코오롱인더스트리와 기아다.", "코오롱인더스트리"),
])
def test_dictionary_names_with_particles(claim, name):
    assert any(name in g for g in groups(claim, "케이산업"))


@pytest.mark.parametrize("claim,passage,expect", [
    ("건설 부문은 아파트를 짓는다.", "건설사업부문은 아파트를 시공합니다.", True),    # 부문명 변형
    ("건설사업부문은 아파트를 짓는다.", "건설 부문은 아파트를 시공합니다.", True),
    ("선재사업부문은 서울사무소에서 판다.", "선재사업부는 서울사무소에서 판매합니다.", True),
    ("단조사업부문은 서울사무소에서 판다.", "선재사업부문은 서울사무소에서 판매합니다.", False),
])
def test_division_name_variants(claim, passage, expect):
    assert ok(claim, passage, "케이산업") is expect


# --- 교체 문장은 주체 확인에서 떨어진다 --------------------------------------------------------------------------
@pytest.mark.parametrize("claim,passage", [
    ("고려제강은 2006년 품질경영시스템 인증을 획득했다.", "당사는 2006년 국제 자동차분야 품질경영시스템 인증을 획득하였습니다."),
    ("건설 부문은 커피와 커피머신을 판매한다.", "커피 부문은 커피와 커피머신 등 상품을 판매하는 사업을 영위합니다."),
    ("배우 정해인과 로운은 큐브엔터 소속 연예인이다.", "당사 소속 연예인으로 정해인, 로운 등이 있습니다."),
    ("삼성과 공급 계약을 맺었다.", "삼성전자와 공급 계약을 체결하였습니다."),          # 경계: '삼성' ≠ '삼성전자'
])
def test_swapped_subject_fails(claim, passage):
    assert not ok(claim, passage)


@pytest.mark.parametrize("claim,passage", [
    ("커피 부문은 커피와 커피머신을 판매한다.", "커피 부문은 커피와 커피머신 등 상품을 판매하는 사업을 영위합니다."),
    ("대원산업은 2006년 품질경영시스템 인증을 획득했다.", "당사는 2006년 국제 자동차분야 품질경영시스템 인증을 획득하였습니다."),
    ("이 부문에서도 원재료가 따로 필요하지 않다.", "MD 상품은 외주 생산으로 조달합니다."),   # 후보 없음 → 통과
])
def test_true_subject_passes(claim, passage):
    assert ok(claim, passage)


# --- 이름 표기 변형: 같은 이름이면 통과해야 한다(오탐 방지) ------------------------------------------------------
@pytest.mark.parametrize("claim,passage", [
    ("㈜한빛소재와 공급 계약을 맺었다.", "한빛소재(주)와 공급계약을 체결하였습니다."),         # ㈜ ↔ (주)
    ("한빛소재 주식회사와 공급 계약을 맺었다.", "주식회사 한빛소재와 공급계약을 체결하였습니다."),  # 주식회사 앞뒤
    ("(주)한빛소재는 부품을 공급한다.", "한빛소재 주식회사는 부품을 공급합니다."),
    ("Hanwha Ocean과 공급 계약을 맺었다.", "Hanwha Ocean Co., Ltd.와 공급계약을 체결하였습니다."),  # 영문 법인 접미
    ("SYSTEMEVER를 공급한다.", "클라우드 ERP SystemEver를 공급합니다."),                    # 라틴 대소문자
    ("고려제강과 거래한다.", "주요 매입처는 고려 제강입니다."),                              # 문단 쪽 띄어쓰기
    ("고려 제강과 거래한다.", "주요 매입처는 고려제강입니다."),                              # 주장 쪽 띄어쓰기
    ("단조사업부문은 영업소로 지역을 나누어 판매한다.", "단조사업 부문은 서울사무소와 영업소로 판매합니다."),
    ("INCHEON 공장에서 생산한다.", "Incheon 공장에서 생산합니다."),                        # 'Inc'를 법인 접미로 지우지 않는다
])
def test_notation_variants_pass(claim, passage):
    assert ok(claim, passage)


def test_abbreviation_defined_in_passages_passes():
    """문단 묶음 안 괄호 정의(이하 'FNC')로 약칭과 전체 이름을 같은 것으로 본다. 정의가 다른 문단에 있어도 된다."""
    passages = ["에프엔씨엔터테인먼트(이하 'FNC')는 2006년 설립되었습니다.",
                "에프엔씨엔터테인먼트 소속 배우로 정해인이 있습니다.",
                "주요 원재료는 없습니다."]
    fnc = sj.CompanyNames(["에프엔씨엔터테인먼트"])
    assert sj.subject_valid("FNC 소속 배우가 있다.", passages, "케이스튜디오", names=fnc) == [True, True, False]
    assert sj.subject_valid("에프엔씨엔터테인먼트는 2006년 설립되었다.", ["FNC(에프엔씨엔터테인먼트)는 상장사다.", "FNC는 2006년 설립"],
                            "케이스튜디오", names=fnc) == [True, True]


def test_undefined_abbreviation_is_known_false_negative():
    """문단에 정의가 없는 약칭·음역(SK ↔ 에스케이)은 못 맞춘다. A-3 c3(표기 변형 참) 주장으로 이 오탐을 잰다."""
    assert not ok("SK하이닉스에 메모리를 납품한다.", "에스케이하이닉스에 메모리를 납품합니다.")


def test_missing_subjects_lists_failed_groups():
    claim = "고려제강은 ㈜두리소재와 거래한다."
    passage = "고려제강은 ㈜한빛소재와 거래합니다."
    assert sj.missing_subjects(claim, passage, CO, names=NAMES) == ["두리소재"]


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
