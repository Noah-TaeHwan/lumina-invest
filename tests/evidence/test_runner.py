# tests/evidence/test_runner.py
"""판정 실행기(spec 5·7절): 가짜 JEV 클라이언트 또는 가짜 전송 + 메모리 Redis로만 돈다."""
import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from app.lib import jev, jev_service
from app.services.evidence import runner as rn
from app.services.evidence.judge import QUESTION_SHA

NOW = datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc)
CO = "삼성전자"
PASSAGES = ["회사는 메모리 반도체와 스마트폰을 생산한다.",
            "2025년 영업이익은 32조 7,260억원이다.",
            "주요 원재료는 웨이퍼다."]


def _claim(state: str) -> str:
    return next(ln[len("[Claim] "):] for ln in state.splitlines() if ln.startswith("[Claim] "))


class FakeClient:
    """주장 문장마다 정한 동작을 돌려준다. 동시 실행 수 최댓값을 잰다."""

    def __init__(self, behave=None, delay=0.0):
        self.behave = behave or (lambda claim, n: ("probs", [(0.8, 0.05)] * n))
        self.delay = delay
        self.calls: list[str] = []
        self.active = self.max_active = 0

    async def ask(self, state, questions, *, user_id, log_ctx=None, usage=None):
        claim = _claim(state)
        self.calls.append(claim)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            kind, arg = self.behave(claim, len(questions))
            delay = arg if kind == "sleep" else self.delay
            await asyncio.sleep(delay)
            if kind == "sleep":
                kind, arg = "probs", [(0.8, 0.05)] * len(questions)
            if kind == "raise":
                raise RuntimeError("boom")
            if kind == "fail":
                return jev_service.ServiceJevResult(jev.request_key(state, questions), False, None, 1.0, 0, 2,
                                                    "x", arg[0], arg[1])
            answers = {f"p{j}": {"supports": s, "contradicts": c, "says_nothing": round(1 - s - c, 6)}
                       for j, (s, c) in enumerate(arg, 1)}
            return jev_service.ServiceJevResult(jev.request_key(state, questions), True, answers, 1.0, 100, 1,
                                                None, None, 200)
        finally:
            self.active -= 1


def _run(client, answer, *, redis=None, policy=rn.A2_PROVISIONAL, passages=PASSAGES, user_id="u1"):
    async def go():
        quota = jev_service.Quota(redis if redis is not None else _Room(), now=lambda: NOW)
        return await rn.Runner(client, quota).run(company=CO, answer=answer, passages=passages,
                                                  user_id=user_id, policy=policy)
    return asyncio.run(go())


class _Room:
    """한도가 늘 남는 Redis."""

    async def get(self, key):
        return None


def _answer(n: int) -> str:
    return " ".join(f"회사는 제품 {i}번을 만든다." for i in range(1, n + 1))


def test_provisional_policy_values():
    p = rn.A2_PROVISIONAL
    assert (p.version, p.tau_s, p.tau_c, p.theta_low, p.theta_high) == ("a2-provisional-2", 0.70, 0.35, None, None)
    assert (p.max_claims, p.concurrency, p.deadline_s) == (8, 3, 8.0)


def test_concurrency_limit_is_respected():
    client = FakeClient(delay=0.02)
    res = _run(client, _answer(7))
    assert client.max_active == 3 and len(client.calls) == 7 and res.status == "done"


def test_deadline_leaves_slow_claims_unjudged():
    slow = "회사는 제품 2번을 만든다."
    client = FakeClient(lambda claim, n: ("sleep", 5.0) if claim == slow else ("probs", [(0.8, 0.05)] * n))
    policy = rn.Policy("t", 0.70, 0.35, deadline_s=0.2)
    res = _run(client, _answer(3), policy=policy)
    by = {c.text: c for c in res.claims}
    assert by[slow].status == "unjudged" and by[slow].reason == "deadline" and by[slow].route == "jev"
    assert by["회사는 제품 1번을 만든다."].status != "unjudged" and by["회사는 제품 1번을 만든다."].reason is None
    assert res.status == "partial" and res.error_code == "deadline"


def test_claim_cap_eight():
    client = FakeClient()
    res = _run(client, _answer(10))
    assert len(client.calls) == 8
    assert [c.reason for c in res.claims[8:]] == ["claim_cap", "claim_cap"]
    assert all(c.status == "unjudged" for c in res.claims[8:])
    assert res.status == "done"


def test_sys_decision_mapping_and_source():
    table = {
        "회사는 메모리 반도체를 만든다.": [(0.2, 0.0), (0.1, 0.0), (0.9, 0.0)],
        "회사는 자동차를 만든다.": [(0.1, 0.6), (0.0, 0.0), (0.0, 0.0)],
        "회사는 웨이퍼를 수입한다.": [(0.3, 0.1), (0.2, 0.1), (0.69, 0.1)],
    }
    client = FakeClient(lambda claim, n: ("probs", table[claim]))
    res = _run(client, " ".join(table))
    st = [(c.status, c.source_idx, c.route) for c in res.claims]
    assert st == [("supported", 2, "jev"), ("contradicted", 0, "jev"), ("no_evidence", None, "jev")]
    c0 = res.claims[0]
    assert c0.s == [0.2, 0.1, 0.9] and c0.c == [0.0, 0.0, 0.0] and c0.number_ok == [True, True, True]
    assert c0.jev_request_key and c0.attempts == 1 and isinstance(c0.lex, float)
    assert res.calls == 3 and res.input_tokens == 300 and res.policy_version == "a2-provisional-2"
    assert res.jev_model == jev.MODEL and res.question_sha == QUESTION_SHA


def test_number_check_failing_passage_cannot_support():
    client = FakeClient(lambda claim, n: ("probs", [(0.0, 0.0), (0.95, 0.0), (0.0, 0.0)]))
    res = _run(client, "2025년 영업이익은 40조원이다.")
    c = res.claims[0]
    assert c.number_ok[1] is False and c.status == "no_evidence"


def test_not_claim_sentences_are_not_sent():
    client = FakeClient()
    res = _run(client, "회사는 반도체를 만든다. 배당 정책은 알 수 없습니다. 더 궁금한 점이 있나요?")
    assert [c.status for c in res.claims] == ["supported", "not_claim", "not_claim"]
    assert [c.route for c in res.claims] == ["jev", "rule_not_claim", "rule_not_claim"]
    assert client.calls == ["회사는 반도체를 만든다."]
    assert [(c.start, c.end) for c in res.claims][0] == (0, 13)


def test_only_not_claims_is_done_without_calls(down_redis):
    client = FakeClient()
    res = _run(client, "알 수 없습니다. 무엇이 궁금한가요?", redis=down_redis)  # 한도 확인도 하지 않는다
    assert res.status == "done" and client.calls == []


def test_lexical_tier_routes():
    policy = rn.Policy("tier", 0.70, 0.35, theta_low=0.2, theta_high=0.8)
    passages = PASSAGES + [f"{CO}는 메모리 반도체와 스마트폰을 생산한다."]
    answer = "전혀 관계없는 문장입니다. 삼성전자는 메모리 반도체와 스마트폰을 생산한다. 회사의 메모리 판매가 늘었다."
    client = FakeClient()
    res = _run(client, answer, policy=policy, passages=passages)
    assert [(c.route, c.status) for c in res.claims] == [
        ("lex_low", "no_evidence"), ("lex_high", "supported"), ("jev", "supported")]
    assert res.claims[1].source_idx == 3 and res.claims[1].s is None
    assert client.calls == ["회사의 메모리 판매가 늘었다."]


def test_lex_high_requires_claim_names_in_passage():
    """spec 6.1: 주장의 회사명 후보(여기서는 LG전자)가 근거 문단에 그대로 있어야 상단 구간이다."""
    policy = rn.Policy("tier", 0.70, 0.35, theta_high=0.8)
    client = FakeClient()
    res = _run(client, "회사는 메모리 반도체와 스마트폰을 생산한다. LG전자는 메모리 반도체와 스마트폰을 생산한다.",
               policy=policy)
    assert [c.route for c in res.claims] == ["lex_high", "jev"]
    assert client.calls == ["LG전자는 메모리 반도체와 스마트폰을 생산한다."]


def test_pii_skips_whole_run():
    client = FakeClient()
    res = _run(client, "회사는 반도체를 만든다. 문의는 010-1234-5678로 하세요.")
    assert res.status == "skipped" and res.error_code == "pii" and client.calls == []
    assert all(c.status in ("unjudged", "not_claim") for c in res.claims)


def test_limited_when_remaining_below_estimate(fake_redis):
    fake_redis.data["evidence:quota:20261002:user:u1:calls"] = "149"
    client = FakeClient()
    res = _run(client, _answer(2), redis=fake_redis)
    assert res.status == "limited" and res.error_code == "cap_user" and client.calls == []
    assert {c.reason for c in res.claims} == {"cap_user"}


def test_global_cap_is_limited(fake_redis):
    fake_redis.data["evidence:quota:20261002:global:tokens"] = "2999999"
    res = _run(FakeClient(), _answer(1), redis=fake_redis)
    assert res.status == "limited" and res.error_code == "cap_global"


def test_redis_down_fails_without_calls(down_redis):
    client = FakeClient()
    res = _run(client, _answer(2), redis=down_redis)
    assert res.status == "failed" and res.error_code == "quota_unavailable" and client.calls == []


def test_all_claims_failing_is_failed():
    client = FakeClient(lambda claim, n: ("fail", ("http_5xx", 503)))
    res = _run(client, _answer(2))
    assert res.status == "failed" and res.error_code == "http_5xx"
    assert [c.reason for c in res.claims] == ["http_5xx", "http_5xx"]


def test_some_claims_failing_is_partial():
    bad = "회사는 제품 2번을 만든다."
    client = FakeClient(lambda claim, n: ("fail", ("timeout", None)) if claim == bad else ("probs", [(0.8, 0.0)] * n))
    res = _run(client, _answer(3))
    assert res.status == "partial" and res.error_code == "timeout"


def test_429_stops_remaining_claims():
    hit = "회사는 제품 2번을 만든다."
    client = FakeClient(lambda claim, n: ("fail", ("http_429", 429)) if claim == hit else ("probs", [(0.8, 0.0)] * n))
    res = _run(client, _answer(4), policy=rn.Policy("seq", 0.70, 0.35, concurrency=1))
    assert client.calls == ["회사는 제품 1번을 만든다.", hit]
    assert [c.reason for c in res.claims] == [None, "http_429", "rate_limited", "rate_limited"]
    assert res.status == "partial" and res.error_code == "http_429"


def test_429_from_service_client_gives_run_error_http_429(fake_redis, jev_payload):
    async def handler(request):
        return httpx.Response(429, json={})

    async def go():
        quota, client = _real_client(fake_redis, handler)
        return await rn.Runner(client, quota).run(company=CO, answer=_answer(1), passages=PASSAGES, user_id="u1")

    res = asyncio.run(go())
    assert res.status == "failed" and res.error_code == "http_429" and res.claims[0].reason == "http_429"


def test_deadline_cancel_counts_every_sent_request(fake_redis, jev_payload):
    """응답 0.3초, 마감 0.45초, 주장 8개, 동시성 3: 나간 요청 수 = 한도 calls 카운터 = 실행 calls."""
    seen = []

    async def handler(request):
        seen.append(request)
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=jev_payload(json.loads(request.content)["questions"]))

    async def go():
        quota, client = _real_client(fake_redis, handler)
        policy = rn.Policy("t", 0.70, 0.35, deadline_s=0.45)
        return await rn.Runner(client, quota).run(company=CO, answer=_answer(8), passages=PASSAGES,
                                                  user_id="u1", policy=policy)

    res = asyncio.run(go())
    assert len(seen) == 6
    assert fake_redis.data["evidence:quota:20261002:user:u1:calls"] == str(len(seen))
    assert res.calls == len(seen)
    assert res.input_tokens == 3 * 100 + 3 * jev_service.EST_TOKENS_PER_CALL
    assert fake_redis.data["evidence:quota:20261002:user:u1:tokens"] == str(res.input_tokens)
    assert [c.reason for c in res.claims].count("deadline") == 5


def test_401_fails_run_even_after_success():
    hit = "회사는 제품 2번을 만든다."
    client = FakeClient(lambda claim, n: ("fail", ("http_4xx", 401)) if claim == hit else ("probs", [(0.8, 0.0)] * n))
    res = _run(client, _answer(3), policy=rn.Policy("seq", 0.70, 0.35, concurrency=1))
    assert res.status == "failed" and res.error_code == "http_4xx"
    assert res.claims[2].status == "unjudged" and len(client.calls) == 2


def test_unexpected_exception_is_recorded_not_lost():
    client = FakeClient(lambda claim, n: ("raise", None))
    res = _run(client, _answer(1))
    assert res.status == "failed" and res.claims[0].reason == "error"


def test_run_log_line(caplog):
    caplog.set_level(logging.INFO, logger="app.evidence")
    _run(FakeClient(), "회사는 반도체를 만든다. 알 수 없습니다.")
    line = json.loads(next(r.getMessage() for r in caplog.records if r.name == "app.evidence.runner"))
    assert line["status"] == "done" and line["claims"]["total"] == 2 and line["claims"]["not_claim"] == 1
    assert "반도체" not in caplog.text


def _real_client(redis, handler):
    quota = jev_service.Quota(redis, now=lambda: NOW)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return quota, jev_service.ServiceJevClient(redis, quota, api_key="k", client=http, timeout=0.5)


def test_end_to_end_with_service_client_records_quota(fake_redis, jev_payload):
    async def handler(request):
        return httpx.Response(200, json=jev_payload(json.loads(request.content)["questions"], tokens=4000))

    async def go():
        quota, client = _real_client(fake_redis, handler)
        return await rn.Runner(client, quota).run(company=CO, answer=_answer(2), passages=PASSAGES, user_id="u1")

    res = asyncio.run(go())
    assert res.status == "done" and res.calls == 2 and res.input_tokens == 8000
    assert fake_redis.data["evidence:quota:20261002:user:u1:tokens"] == "8000"
    again = asyncio.run(go())
    assert again.cache_hits == 2 and again.calls == 0


def test_same_user_runs_are_serialized_and_second_is_limited(fake_redis, jev_payload):
    fake_redis.data["evidence:quota:20261002:user:u1:calls"] = "147"

    async def handler(request):
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=jev_payload(json.loads(request.content)["questions"]))

    async def go():
        quota, client = _real_client(fake_redis, handler)
        r = rn.Runner(client, quota)
        return await asyncio.gather(
            r.run(company=CO, answer=_answer(2), passages=PASSAGES, user_id="u1"),
            r.run(company=CO, answer="회사는 제품 8번을 만든다. 회사는 제품 9번을 만든다.", passages=PASSAGES,
                  user_id="u1"))

    first, second = asyncio.run(go())
    assert first.status == "done" and second.status == "limited"


def test_rejudge_uses_stored_probabilities_without_calls():
    client = FakeClient(lambda claim, n: ("probs", [(0.75, 0.0), (0.1, 0.0), (0.1, 0.0)]))
    tier = rn.Policy("tier", 0.70, 0.35, theta_low=0.2)
    res = _run(client, "회사는 반도체를 만든다. 전혀 관계없는 문장입니다. 알 수 없습니다.", policy=tier)
    assert [c.route for c in res.claims] == ["jev", "lex_low", "rule_not_claim"]
    n_calls = len(client.calls)

    strict = rn.Policy("strict", 0.80, 0.35)
    again = rn.rejudge(res.claims, strict)
    assert len(client.calls) == n_calls and again.calls == 0 and again.trigger == "rejudge"
    assert [c.status for c in again.claims] == ["no_evidence", "unjudged", "not_claim"]
    assert again.claims[1].reason == "needs_call" and again.policy_version == "strict"
    assert res.claims[0].status == "supported"  # 원본은 바뀌지 않는다

    same_zone = rn.rejudge(res.claims, rn.Policy("tier2", 0.70, 0.35, theta_low=0.3))
    assert [c.status for c in same_zone.claims] == ["supported", "no_evidence", "not_claim"]
    assert same_zone.status == "done"


def test_rejudge_keeps_unjudged_claims_unjudged():
    client = FakeClient(lambda claim, n: ("fail", ("timeout", None)))
    res = _run(client, _answer(1))
    again = rn.rejudge(res.claims, rn.A2_PROVISIONAL)
    assert again.claims[0].status == "unjudged" and again.status == "failed"


def test_runner_module_does_not_import_lab():
    import inspect
    src = inspect.getsource(rn)
    assert "from lab" not in src and "import lab" not in src
