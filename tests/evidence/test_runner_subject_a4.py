# tests/evidence/test_runner_subject_a4.py
"""실험 정책 a4-subject-exp(A-4 spec 7절): ①c 코드 + ② 주체 질문(후속 요청)을 ✅ 필요조건으로 더한다. 꺼진 정책이고
기본 정책(a2-v1)과 A-3 정책은 그대로다. 외부 호출 없음(가짜 JEV)."""
import asyncio
from dataclasses import replace

import pytest

from app.lib import jev, jev_service
from app.services.evidence import records
from app.services.evidence import runner as rn
from app.services.evidence import subject as sj
from app.services.evidence import subject_a4 as sa
from tests.evidence.test_runner import NOW, _claim, _Room

CO = "대원산업"
PASSAGES = ["당사는 2006년 국제 자동차분야 품질경영시스템 인증을 획득하였습니다.",
            "커피 부문은 커피와 커피머신 등 상품을 판매하는 사업을 영위합니다."]
TRUE = "대원산업은 2006년 국제 자동차분야 품질경영시스템 인증을 획득했다."
DIV_SWAP = "건설 부문은 커피와 커피머신 등 상품을 판매하는 사업을 영위합니다."  # 부문 교체: 코드(X0)는 통과, ②가 본다
NAMES = sj.CompanyNames(["고려제강", "대원산업"])
A4 = replace(rn.A4_SUBJECT, tau_d=0.5, subject_signal="p_diff")  # τ_d는 탐색 뒤 사전등록 값(테스트 값)
SAME = {"same_subject": 0.9, "different_subject": 0.05, "unclear": 0.05}
DIFF = {"same_subject": 0.1, "different_subject": 0.8, "unclear": 0.1}


class A4Client:
    """주 판정(p*)과 후속 주체 질문(s*)에 답하는 가짜 JEV. main(claim, n) → [(s, c)], subject(claim, qids) → 확률 목록·
    'fail'·('sleep', 초)."""

    def __init__(self, main=None, subj=None):
        self.main = main or (lambda claim, n: [(0.9, 0.0)] + [(0.1, 0.0)] * (n - 1))
        self.subj = subj or (lambda claim, qids: [SAME] * len(qids))
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def ask(self, state, questions, *, user_id, log_ctx=None, usage=None):
        claim, qids = _claim(state), tuple(questions)
        self.calls.append(("subject" if qids[0].startswith("s") else "main", qids))
        key = jev.request_key(state, questions)
        if qids[0].startswith("s"):
            out = self.subj(claim, qids)
            if isinstance(out, tuple) and out[0] == "sleep":
                if usage is not None:
                    usage.update(calls=1, tokens=50)
                await asyncio.sleep(out[1])
                out = [SAME] * len(qids)
            if out == "fail":
                return jev_service.ServiceJevResult(key, False, None, 1.0, 0, 2, "x", "server_error", 500)
            return jev_service.ServiceJevResult(key, True, dict(zip(qids, out)), 1.0, 40, 1, None, None, 200)
        answers = {f"p{j}": {"supports": s, "contradicts": c, "says_nothing": round(1 - s - c, 6)}
                   for j, (s, c) in enumerate(self.main(claim, len(qids)), 1)}
        return jev_service.ServiceJevResult(key, True, answers, 1.0, 100, 1, None, None, 200)

    def kinds(self):
        return [k for k, _ in self.calls]


def _run(client, answer, *, policy=A4, passages=PASSAGES):
    async def go():
        quota = jev_service.Quota(_Room(), now=lambda: NOW)
        return await rn.Runner(client, quota, company_names=NAMES).run(company=CO, answer=answer, passages=passages,
                                                                      user_id="u1", policy=policy)
    return asyncio.run(go())


def test_default_policy_unchanged():
    assert rn.DEFAULT_POLICY is rn.A2_V1
    assert (rn.A2_V1.subject_question, rn.A2_V1.tau_d, rn.A2_V1.subject_signal) == (False, None, None)
    assert rn.A2_V1.subject_mode == "off" and rn.A3_SUBJECT.subject_mode == "a3-code"
    assert replace(rn.A3_SUBJECT, version=rn.A2_V1.version, subject_check=False) == rn.A2_V1


def test_a4_policy_values():
    p = rn.A4_SUBJECT
    assert (p.version, p.tau_s, p.tau_c, p.theta_low, p.theta_high) == ("a4-subject-exp", 0.85, 0.35, None, 0.95)
    assert (p.subject_check, p.subject_question, p.subject_mode) == (False, True, "a4-code-question")
    assert (p.tau_d, p.subject_signal) == (None, None)  # 사전등록 초안: τ_d·신호는 탐색 뒤에 정한다
    assert replace(p, version=rn.A2_V1.version, subject_question=False) == rn.A2_V1
    assert records.POLICIES[p.version] is p


def test_a4_policy_refuses_to_run_without_registered_tau_d():
    with pytest.raises(ValueError, match="tau_d"):
        _run(A4Client(), TRUE, policy=rn.A4_SUBJECT)


def test_no_candidate_means_no_followup():
    client = A4Client(main=lambda claim, n: [(0.3, 0.0)] * n)
    res = _run(client, TRUE)
    assert res.claims[0].status == "no_evidence" and client.kinds() == ["main"]
    client = A4Client(main=lambda claim, n: [(0.9, 0.0), (0.1, 0.95)])  # 반박이면 후속 없음, ⚠️는 a2-v1 그대로
    assert _run(client, TRUE).claims[0].status == "contradicted" and client.kinds() == ["main"]


def test_supported_needs_question_pass_and_counts_followup_tokens():
    client = A4Client()
    res = _run(client, TRUE)
    c = res.claims[0]
    assert (c.status, c.source_idx, c.route) == ("supported", 0, "jev")
    assert client.calls == [("main", ("p1", "p2")), ("subject", ("s1",))]  # 후보 문단만 다시 번호
    assert c.subject_q == {0: SAME} and c.subject_ok == [True, True]
    assert (res.calls, res.input_tokens) == (2, 140)
    base = _run(A4Client(), TRUE, policy=rn.A2_V1)
    assert base.claims[0].status == "supported" and base.calls == 1  # a2-v1에는 후속이 없다


def test_division_swap_blocked_by_question_not_code():
    client = A4Client(main=lambda claim, n: [(0.1, 0.0), (0.95, 0.0)], subj=lambda claim, q: [DIFF] * len(q))
    res = _run(client, DIV_SWAP)
    c = res.claims[0]
    assert c.subject_ok == [True, True]  # X0: 부문 이름은 코드 후보가 아니다
    assert (c.status, c.reason) == ("no_evidence", None) and client.kinds() == ["main", "subject"]
    assert _run(A4Client(main=lambda claim, n: [(0.1, 0.0), (0.95, 0.0)]), DIV_SWAP, policy=rn.A2_V1) \
        .claims[0].status == "supported"


def test_company_swap_blocked_by_code_without_followup():
    client = A4Client()
    res = _run(client, "고려제강은 2006년 국제 자동차분야 품질경영시스템 인증을 획득했다.")
    assert res.claims[0].status == "no_evidence" and res.claims[0].subject_ok == [False, False]
    assert client.kinds() == ["main", "subject"]  # 후보는 a2-v1 기준(숫자·τ_s)이라 후속은 나간다


def test_lex_high_claim_gets_one_followup_and_no_main_call():
    loose = replace(A4, theta_high=0.8)
    claim = "커피 부문은 커피와 커피머신 등 상품을 판매하는 사업을 영위합니다."
    client = A4Client()
    res = _run(client, claim, policy=loose)
    c = res.claims[0]
    assert (c.route, c.status, c.source_idx) == ("lex_high", "supported", 1)
    assert client.calls == [("subject", ("s1",))] and res.calls == 1 and res.input_tokens == 40


def test_lex_high_question_fail_goes_to_main_path():
    loose = replace(A4, theta_high=0.8)
    client = A4Client(main=lambda claim, n: [(0.1, 0.0), (0.95, 0.0)], subj=lambda claim, q: [DIFF] * len(q))
    res = _run(client, DIV_SWAP, policy=loose)
    assert (res.claims[0].route, res.claims[0].status) == ("jev", "no_evidence")
    assert client.kinds() == ["subject", "main"]  # 이미 물은 문단(최고 점수 문단)은 다시 묻지 않는다


def test_followup_failure_is_subject_unjudged():
    client = A4Client(subj=lambda claim, q: "fail")
    res = _run(client, TRUE)
    c = res.claims[0]
    assert (c.status, c.reason, c.source_idx) == ("no_evidence", sa.SUBJECT_UNJUDGED, None)
    assert res.status == "done"  # ❔은 판정 결과다(미판정 실패가 아니다)


def test_followup_past_deadline_is_subject_unjudged():
    client = A4Client(subj=lambda claim, q: ("sleep", 0.5))
    res = _run(client, TRUE, policy=replace(A4, deadline_s=0.1))
    c = res.claims[0]
    assert (c.status, c.reason) == ("no_evidence", sa.SUBJECT_UNJUDGED)
    assert res.input_tokens == 150  # 마감으로 취소된 후속 호출의 토큰도 남긴다


def test_rejudge_into_a4_needs_call():
    """저장 행에는 코드 확인·후속 응답이 없다(꺼진 실험 정책이라 DB 열을 만들지 않았다)."""
    res = _run(A4Client(), TRUE, policy=rn.A2_V1)
    again = rn.rejudge(res.claims, A4)
    assert (again.claims[0].status, again.claims[0].reason) == ("unjudged", "needs_call")
    exp = _run(A4Client(), TRUE)
    assert rn.rejudge(exp.claims, A4).claims[0].status == "supported"  # 메모리의 후속 응답이 있으면 그대로 적용
    blocked = rn.rejudge(exp.claims, replace(A4, tau_d=0.01))
    assert blocked.claims[0].status == "no_evidence"


def test_a2_and_a3_runs_unaffected_by_a4_fields():
    res = _run(A4Client(), TRUE, policy=rn.A3_SUBJECT)
    assert res.claims[0].status == "supported" and res.claims[0].subject_q is None and res.calls == 1
