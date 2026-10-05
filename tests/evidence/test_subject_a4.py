# tests/evidence/test_subject_a4.py
"""A-4 주체 판정(A-4 spec 3절): ①c 코드 규칙(X0·N1·N2, 보조 팔 A의 X1·X2), ② JEV 주체 질문(후속 요청) 빌더·후보
문단·신호, 판정 a4-subject-exp(C)와 보조 팔 A·B. 외부 호출 없음.

예문은 합성 문장이다. 'A-3 손실' 예문은 A-3 확인 세트 감사 목록(results/a3-check.json removed, 공개)의 주장 문장과 해석 문서에
적힌 문단 표기를 본떴다. A-3 확인 세트는 A-4에서 탐색 데이터이므로, 이 예문을 막는다는 테스트는 **탐색 증거**이지 확인
증거가 아니다(A-4 spec 3.1).
"""
import hashlib
import json
import random
from pathlib import Path

import pytest

from app.services.evidence import judge, subject
from app.services.evidence import subject_a4 as sa
from app.services.evidence.judge import Judgement, sys_decision

REPO = Path(__file__).resolve().parents[2]
CO = "대원산업"
NAMES = subject.CompanyNames(["고려제강", "대원산업", "SK하이닉스", "삼성SDI"])


def c_ok(claim, passage, company=CO, arm="c"):
    return sa.code_valid(claim, [passage], company, names=NAMES, arm=arm)[0]


def a3_ok(claim, passage, company=CO):
    return subject.subject_valid(claim, [passage], company, names=NAMES)[0]


# --- ①c 코드 규칙(3.1) ---------------------------------------------------------------------------------------
def test_n1_legal_mark_becomes_space_in_passage():
    claim = "종속회사인 (주)모모랩스, (주)모모부산, (주)모모대구, (주)모모수원은 모두 의료서비스 컨설팅을 주요 사업으로 한다."
    passage = "연결대상 종속회사 (주)모모랩스(주)모모부산(주)모모대구(주)모모수원은 의료서비스 컨설팅을 영위합니다."
    assert not a3_ok(claim, passage) and c_ok(claim, passage) and c_ok(claim, passage, arm="a")


@pytest.mark.parametrize("claim,passage", [
    ("RF 부문에서는 커넥터와 케이블 어셈블리를 제조한다.", "RF사업 부문은 커넥터와 케이블 어셈블리를 제조합니다."),
    ("당사의 주력 제품은 모바일 기기를 대표하는 IT 부품이다.", "당사의 주력 제품은 모바일 기기용 IT부품입니다."),
])
def test_n2_latin_to_hangul_is_a_right_boundary(claim, passage):
    assert not a3_ok(claim, passage) and c_ok(claim, passage)


def test_n2_hangul_to_latin_is_not_a_boundary():
    """왼쪽 경계는 그대로: 'SDI'는 '삼성SDI' 안에서 찾지 않는다."""
    assert not c_ok("SDI는 배터리를 생산한다.", "삼성SDI는 배터리를 생산합니다.", company="케이산업")


def test_n2_known_cost_sk_found_inside_sk_hynix():
    """알려진 대가(spec 3.1 N2): 주장 'SK'가 문단 'SK하이닉스'에서 찾아져 계열사 약칭 교체를 코드로는 놓칠 수 있다."""
    claim, passage = "SK는 메모리 반도체를 생산한다.", "SK하이닉스는 메모리 반도체를 생산합니다."
    assert not a3_ok(claim, passage, company="케이산업") and c_ok(claim, passage, company="케이산업")


def test_x0_division_names_are_not_code_candidates():
    claim = "토목사업부문은 도로와 항만 공사를 수행한다."
    passage = "당사의 건축부문은 도로와 항만 공사를 수행합니다."
    assert [g for g in sa.code_groups(claim, CO, names=NAMES, arm="c")] == []
    assert sa.code_groups(claim, CO, names=NAMES, arm="a3")  # A-3 규칙에서는 부문 이름이 후보다
    assert not a3_ok(claim, passage) and c_ok(claim, passage)
    assert not c_ok(claim, passage, arm="a")  # 보조 팔 A는 (e)를 남긴다: '토목'은 A의 알려진 한계
    assert c_ok(claim, passage, arm="a3x0")  # 감사 팔: A-3 규칙 + X0만


@pytest.mark.parametrize("claim", ["당사는 두 가지 사업부문을 영위한다.", "당사는 몇 가지 사업부문을 영위한다.",
                                   "당사는 여러 가지 사업부문을 영위한다."])
def test_x1_quantity_before_spaced_division_is_not_a_name(claim):
    assert sa.code_groups(claim, CO, names=NAMES, arm="a3")
    assert sa.code_groups(claim, CO, names=NAMES, arm="a") == []


@pytest.mark.parametrize("claim", ["공시상 사업부문은 두 개로 나뉜다.", "지역별 사업부문 매출이 늘었다.",
                                   "국가간 사업부문 이전이 있었다."])
def test_x2_adverbial_suffix_core_is_not_a_name(claim):
    assert sa.code_groups(claim, CO, names=NAMES, arm="a3")
    assert sa.code_groups(claim, CO, names=NAMES, arm="a") == []


def test_x1_x2_keep_real_division_names_in_arm_a():
    assert sa.code_groups("건설 부문은 도로를 시공한다.", CO, names=NAMES, arm="a") == [sa.subject._division_alts("건설")]


# 탐색 증거(A-3 확인 세트 = A-4 탐색 데이터): A-3 손실 6건의 주장 문장이 C 코드 확인을 통과한다. 효과 주장은 새 확인 세트로만.
A3_LOSSES = [
    ("토목", "한신공영", "토목사업부문은 주택 및 빌딩 건설, 도로, 항만, 사회간접자본 및 산업 생산 기반 시설의 확충, 국토 개발, "
     "국제 개발사업 등을 포함하는 광범위한 산업입니다.",
     "건설업은 주택 및 빌딩 건설, 도로, 항만, 사회간접자본 시설을 포함하는 광범위한 산업입니다. 당사는 건축부문을 영위합니다."),
    ("가지", "텔콘RF제약", "텔콘RF제약은 RF 부문과 제약ㆍ바이오 부문 두 가지 사업부문을 영위하고 있습니다.",
     "당사는 RF 부문과 제약ㆍ바이오 부문을 영위하고 있습니다."),
    ("RF", "텔콘RF제약", "RF 부문에서는 기타 무선 통신장비제조에 사용되는 커넥터와 케이블 어셈블리 등의 제조를 주요 사업으로 "
     "영위하고 있으며, 신규사업으로는 광 사업장 확대를 추진하고 있습니다.",
     "RF사업 부문은 무선 통신장비제조에 사용되는 커넥터와 케이블 어셈블리를 제조하며 광 사업장 확대를 추진합니다."),
    ("모모", "모모", "종속회사인 (주)모모랩스, (주)모모부산, (주)모모대구, (주)모모수원은 모두 의료서비스 컨설팅을 주요 사업으로 "
     "하고 있습니다.", "종속회사 (주)모모랩스(주)모모부산(주)모모대구(주)모모수원은 의료서비스 컨설팅을 영위합니다."),
    ("공시상", "파인엠텍", "공시상 사업부문은 '폴더블 디스플레이용 융복합 정밀 기구부품' 부문과 '2차전지용 기구부품(ESS 등)' "
     "부문으로 나뉘어 있습니다.", "당사의 사업부문은 '폴더블 디스플레이용 융복합 정밀 기구부품' 부문과 "
     "'2차전지용 기구부품(ESS 등)' 부문입니다."),
    ("IT", "파인엠텍", "파인엠텍의 주력 제품은 모바일 기기를 대표하는 IT 부품입니다.",
     "당사의 주력 제품은 모바일 기기용 IT부품입니다."),
]


@pytest.mark.parametrize("tag,company,claim,passage", A3_LOSSES, ids=[x[0] for x in A3_LOSSES])
def test_exploration_evidence_a3_losses_pass_c_code_check(tag, company, claim, passage):
    """탐색 증거: A-3 규칙에서는 떨어지고 C 코드 규칙(X0·N1·N2)에서는 통과한다."""
    assert not a3_ok(claim, passage, company=company)
    assert c_ok(claim, passage, company=company)


def test_company_swap_still_fails_code_check():
    swap, passage = "고려제강은 2006년 품질경영시스템 인증을 획득했다.", "당사는 2006년 품질경영시스템 인증을 획득하였습니다."
    for arm in ("c", "a", "a3x0", "a3"):
        assert not c_ok(swap, passage, arm=arm), arm
    assert c_ok("대원산업은 2006년 품질경영시스템 인증을 획득했다.", passage)


def test_importing_subject_a4_leaves_a3_rules_unchanged():
    """subject_a4는 subject를 가져다 쓰기만 한다(바꾸지 않는다): A-3 규칙은 여전히 'RF사업'에서 'RF'를 못 찾는다."""
    assert not subject.subject_ok("RF 부문은 커넥터를 만든다.", "RF사업 부문은 커넥터를 만듭니다.", CO, names=NAMES)
    assert not subject._found("모모랩스", subject._strip_legal("(주)모모랩스(주)모모부산"))


# --- ② 주체 질문(3.2) ----------------------------------------------------------------------------------------
def test_subject_question_schema_and_hash():
    st, qs, qmap = sa.build_followup("대원산업", "주장", ["p0", "p1", "p2"], [0, 2])
    assert list(qs) == ["s1", "s2"] and qmap == {"s1": 0, "s2": 2}
    for j, q in enumerate(qs.values(), 1):
        assert q["type"] == "choice" and set(q["criteria"]) == {"same_subject", "different_subject", "unclear"}
        assert f"[Passage {j}]" in q["instructions"] and "'당사'" in q["instructions"]
    want = hashlib.sha256(json.dumps([sa.SUBJECT_INSTRUCTIONS, sa.SUBJECT_CRITERIA], ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()
    assert sa.SUBJECT_QUESTION_SHA == want
    assert sa.PROMPTS[sa.PROMPT_VERSION] == (sa.SUBJECT_INSTRUCTIONS, sa.SUBJECT_CRITERIA)
    assert sa.SUBJECT_QUESTION_SHA != judge.QUESTION_SHA  # 주 판정 질문과 문구를 나눴다


def test_main_judge_question_unchanged():
    pre = json.loads((REPO / "lab/evidence/prereg_a3.json").read_text())
    assert judge.QUESTION_SHA == pre["judge_question_sha"]


def test_followup_state_renumbers_candidates_only():
    ps = [f"문단{i}" for i in range(8)]
    st, qs, qmap = sa.build_followup("대원산업", "대원산업은 성장했다.", ps, [2, 5])
    assert st == judge.build_state("대원산업", "대원산업은 성장했다.", ["문단2", "문단5"])
    assert "[Passage 1] 문단2" in st and "[Passage 3]" not in st
    st8, qs8, qmap8 = sa.build_followup("대원산업", "x", ps, [2, 5], context="all")  # 결정 2-2 전환형
    assert st8 == judge.build_state("대원산업", "x", ps) and qmap8 == {"s3": 2, "s6": 5}
    assert "[Passage 3]" in qs8["s3"]["instructions"]


def test_candidate_passages():
    s, c, valid = [0.9, 0.95, 0.5, 0.86], [0.0] * 4, [True, False, True, True]
    assert sa.candidate_passages(s, c, valid, 0.85, 0.35) == [0, 3]  # 숫자 실패·s < τ_s 제외
    assert sa.candidate_passages(s, [0.0, 0.0, 0.0, 0.96], valid, 0.85, 0.35) == []  # 반박이면 후속 없음
    assert sa.candidate_passages([0.5] * 4, c, valid, 0.85, 0.35) == []  # ✅ 후보 없음
    assert sa.candidate_passages(s, c, valid, 0.85, 0.35, best=2) == [0, 2, 3]  # 상단 구간 최고 문단 포함
    assert sa.candidate_passages(None, None, valid, 0.85, 0.35, best=2) == [2]  # 주 판정 전 상단 구간
    assert sa.candidate_passages(s, c, valid, 0.85, 0.35, ok=False) == []  # 미판정


@pytest.mark.parametrize("signal,probs,tau,ok", [
    ("p_diff", {"same_subject": 0.3, "different_subject": 0.3, "unclear": 0.4}, 0.5, True),
    ("p_diff_unclear", {"same_subject": 0.3, "different_subject": 0.3, "unclear": 0.4}, 0.5, False),
    ("p_diff", {"same_subject": 0.5, "different_subject": 0.5, "unclear": 0.0}, 0.5, False),  # τ_d 이상이면 막음
    ("p_diff", None, 0.5, False),  # 묻지 않은 문단은 통과가 아니다
])
def test_question_signal(signal, probs, tau, ok):
    assert sa.question_pass(probs, signal, tau) is ok


def test_question_signal_rejects_unknown():
    with pytest.raises(ValueError):
        sa.question_pass({"different_subject": 0.1}, "p_same", 0.5)
    assert sa.SIGNALS == ("p_diff", "p_diff_unclear")


def test_q_mask_from_followup_answers():
    answers = {"s1": {"same_subject": 0.9, "different_subject": 0.05, "unclear": 0.05},
               "s2": {"same_subject": 0.1, "different_subject": 0.8, "unclear": 0.1}}
    q = sa.followup_probs(answers, {"s1": 0, "s2": 2})
    assert q == {0: answers["s1"], 2: answers["s2"]}
    assert sa.q_mask(q, 3, "p_diff", 0.5) == [True, False, False]
    assert sa.q_mask(None, 3, "p_diff", 0.5) is None


# --- 판정(3.3) ----------------------------------------------------------------------------------------------
def test_c_needs_number_code_and_question():
    s, c, valid = [0.9, 0.95], [0.0, 0.0], [True, True]
    assert sa.decide(s, c, valid, [True, True], [True, False], 0.85, 0.35) == ("supported", 0, 0.9)
    assert sa.decide(s, c, valid, [False, True], [True, False], 0.85, 0.35) == ("no_evidence", None, 0.0)
    assert sa.decide(s, c, valid, None, None, 0.85, 0.35, question=False, code=False) == ("supported", 1, 0.95)


def test_followup_failure_is_no_evidence_not_silent_supported():
    s, c, valid = [0.9], [0.0], [True]
    assert sa.decide(s, c, valid, [True], None, 0.85, 0.35) == ("no_evidence", None, 0.0)
    assert sa.decide(s, [0.95], valid, [True], None, 0.85, 0.35)[0] == "contradicted"  # ⚠️는 a2-v1 그대로
    assert sa.SUBJECT_UNJUDGED == "subject_unjudged"


ARMS = {"C": dict(code=True, question=True), "A": dict(code=True, question=False),
        "B": dict(code=False, question=True), "a3": dict(code=True, question=False)}


def test_all_arms_are_pure_restrictions_of_a2_v1():
    """모든 조합에서 C·A·B(·a3) ✅ ⊆ a2-v1 ✅, JEV 경로 ⚠️는 a2-v1과 같다(무작위 표본, 시드 고정)."""
    rng = random.Random(20261305)
    for _ in range(4000):
        n = rng.randint(1, 8)
        s = [rng.choice([0.0, 0.3, 0.84, 0.85, 0.9, 1.0, rng.random()]) for _ in range(n)]
        c = [rng.choice([0.0, 0.34, 0.35, 0.6, rng.random()]) for _ in range(n)]
        valid = [rng.random() < 0.8 for _ in range(n)]
        code = [rng.random() < 0.7 for _ in range(n)]
        q = None if rng.random() < 0.1 else [rng.random() < 0.7 for _ in range(n)]
        base = sys_decision(Judgement(s, c, True, 1), valid, 0.85, 0.35)
        for name, kw in ARMS.items():
            got = sa.decide(s, c, valid, code if kw["code"] else None, q if kw["question"] else None, 0.85, 0.35, **kw)
            if got[0] == "supported":
                assert base[0] == "supported", name
            assert (got[0] == "contradicted") == (base[0] == "contradicted"), name
            if base[0] != "supported":  # 점수는 조건을 통과한 문단의 최댓값이라 다를 수 있다(A-3 S_VS와 같다)
                assert got[:2] == base[:2], name
        lex, high, best = rng.random(), rng.random() < 0.5, rng.randrange(n)
        r0 = sa.route(lex, high, best, None, None, None, 0.95, code=False, question=False)  # a2-v1
        for kw in ARMS.values():
            r = sa.route(lex, high, best, code if kw["code"] else None, q if kw["question"] else None, None, 0.95, **kw)
            assert r != "lex_high" or r0 == "lex_high"


def test_route_lex_high_needs_code_and_question_on_best():
    assert sa.route(0.99, True, 0, [True], [True], None, 0.95) == "lex_high"
    assert sa.route(0.99, True, 0, [True], [False], None, 0.95) == "jev"
    assert sa.route(0.99, True, 0, [False], [True], None, 0.95) == "jev"
    assert sa.route(0.99, True, 0, [True], None, None, 0.95) == "jev"  # 후속 실패
    assert sa.route(0.10, False, None, None, None, 0.2, 0.95) == "lex_low"


def test_hash_target_files_of_a2_a3_unchanged():
    """HEAD에서 a3-report(사전등록 코드 해시 대조)가 깨지지 않게: A-2·A-3 사전등록 해시 대상 파일이 바이트 그대로다."""
    for name in ("prereg_a2.json", "prereg_a3.json"):
        for rel, sha in json.loads((REPO / "lab/evidence" / name).read_text())["code_sha256"].items():
            assert hashlib.sha256((REPO / rel).read_bytes()).hexdigest() == sha, rel
