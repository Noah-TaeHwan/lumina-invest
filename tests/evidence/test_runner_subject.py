# tests/evidence/test_runner_subject.py
"""실험 정책 a3-subject-exp(A-3 spec 4절): 주체 확인을 ✅ 필요조건으로 더한다. 기본 정책(a2-v1)은 그대로다."""
import asyncio
from dataclasses import replace

from app.lib import jev_service
from app.services.evidence import records
from app.services.evidence import runner as rn
from tests.evidence.test_runner import NOW, FakeClient, _Room

CO = "대원산업"
PASSAGES = ["당사는 2006년 국제 자동차분야 품질경영시스템 인증을 획득하였습니다.",
            "커피 부문은 커피와 커피머신 등 상품을 판매하는 사업을 영위합니다."]
SWAP = "고려제강은 2006년 국제 자동차분야 품질경영시스템 인증을 획득했다."
TRUE = "대원산업은 2006년 국제 자동차분야 품질경영시스템 인증을 획득했다."


def _run(client, answer, *, policy, passages=PASSAGES):
    async def go():
        quota = jev_service.Quota(_Room(), now=lambda: NOW)
        return await rn.Runner(client, quota).run(company=CO, answer=answer, passages=passages, user_id="u1",
                                                  policy=policy)
    return asyncio.run(go())


def _probs(claim, n):
    return "probs", [(0.9, 0.0)] + [(0.1, 0.0)] * (n - 1)


def test_default_policy_is_unchanged_and_subject_check_off():
    assert rn.DEFAULT_POLICY is rn.A2_V1 and rn.A2_V1.subject_check is False
    assert rn.Policy("x", 0.7, 0.35).subject_check is False
    assert rn.A2_PROVISIONAL.subject_check is False


def test_a3_policy_is_a2_v1_plus_subject_check():
    p = rn.A3_SUBJECT
    assert (p.version, p.subject_check) == ("a3-subject-exp", True)
    assert replace(p, version=rn.A2_V1.version, subject_check=False) == rn.A2_V1
    assert records.POLICIES[p.version] is p  # 확신도 라벨용 등록(실행은 만들지 않는다)


def test_subject_swap_is_supported_under_a2_v1_but_not_under_a3():
    base = _run(FakeClient(_probs), SWAP, policy=rn.A2_V1, passages=PASSAGES)
    exp = _run(FakeClient(_probs), SWAP, policy=rn.A3_SUBJECT, passages=PASSAGES)
    assert base.claims[0].status == "supported" and base.claims[0].subject_ok is None
    c = exp.claims[0]
    assert (c.status, c.route, c.subject_ok) == ("no_evidence", "jev", [False, False])
    assert exp.policy_version == "a3-subject-exp"


def test_true_claim_about_self_stays_supported_under_a3():
    res = _run(FakeClient(_probs), TRUE, policy=rn.A3_SUBJECT, passages=PASSAGES)
    assert (res.claims[0].status, res.claims[0].source_idx) == ("supported", 0)


def test_lex_high_also_needs_subject_check():
    """상단 구간도 순수 제한: 최고 점수 문단이 주체 확인을 통과해야 JEV 없이 ✅."""
    claim = "건설 부문은 커피와 커피머신 등 상품을 판매하는 사업을 영위합니다."
    loose = rn.Policy("tier", 0.85, 0.35, theta_high=0.8)
    a2 = _run(FakeClient(), claim, policy=loose, passages=PASSAGES)
    client = FakeClient(_probs)
    a3 = _run(client, claim, policy=replace(loose, subject_check=True), passages=PASSAGES)
    assert a2.claims[0].route == "lex_high"
    assert a3.claims[0].route == "jev" and client.calls == [claim] and a3.claims[0].status == "no_evidence"


def test_rejudge_into_subject_policy_needs_stored_subject_check():
    """저장 행에는 subject_ok가 없다(꺼진 실험 정책이라 DB 열을 만들지 않았다). 조용히 a2-v1처럼 판정하지 않는다."""
    res = _run(FakeClient(_probs), TRUE, policy=rn.A2_V1, passages=PASSAGES)
    again = rn.rejudge(res.claims, rn.A3_SUBJECT)
    assert (again.claims[0].status, again.claims[0].reason) == ("unjudged", "needs_call")
    exp = _run(FakeClient(_probs), SWAP, policy=rn.A3_SUBJECT, passages=PASSAGES)
    assert rn.rejudge(exp.claims, rn.A3_SUBJECT).claims[0].status == "no_evidence"  # 있으면 그대로 적용


def test_route_and_decide_helpers_are_shared_with_eval():
    """평가(lab/evidence/a3)가 쓰는 함수: 경로(route_claim)와 JEV 판정(decide_claim)."""
    assert rn.route_claim(rn.A3_SUBJECT, 0.99, True, 0, [False]) == "jev"
    assert rn.route_claim(rn.A2_V1, 0.99, True, 0, None) == "lex_high"
    assert rn.decide_claim(rn.A2_V1, [0.9], [0.0], [True], None) == ("supported", 0, 0.9)
    assert rn.decide_claim(rn.A3_SUBJECT, [0.9], [0.0], [True], [False]) == ("no_evidence", None, 0.0)

