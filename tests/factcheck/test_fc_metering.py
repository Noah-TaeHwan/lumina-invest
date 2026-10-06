# tests/factcheck/test_fc_metering.py
"""팩트체커 JEV 계량(비용 상한): 호출별 사용량 원장과 요청당 상한.

- 원장(Ledger) 없이는 유료 호출이 나가지 않는다(계량 안 된 경로 없음).
- 요청 상한 REQUEST_TOKEN_CAP(28,000, 배포 전 검수 A5) — 본문 UTF-8 바이트 기준 상한이 이를 넘는 요청은 보내지 않는다.
- 호출 하나의 몫 = SLOT_TOKENS(상한 × 최대 시도). 호출 전에 원장에서 잡고 끝나면 보고된 사용량으로 바꾼다.
  모르는 사용량(실패 시도·예외·취소·비정상 보고)은 그 호출의 몫 그대로(요청 바이트 추정이 아니라, B6).
- 사용량 검증은 실제 HTTP 응답의 원시 값으로 한다: true·1.9·"1"은 비정상(B5, ServiceJevClient의 int() 변환 전).
- 요청이 하나도 나가지 않은 호출(API 키 없음·캐시)은 0(A4).
- 원장을 닫은 뒤에는 새 호출도 환급도 없다.
DB가 필요 없다.
"""
import asyncio
import json

import httpx
import pytest

from app.lib import jev
from app.lib.jev_service import ServiceJevResult
from app.services.factcheck import metering as fm
from tests.factcheck.fc_support import FakeServiceJev

STATE, Q = "회사: 삼성전자\n주장: 2025년 매출은 300조원이다.", {"p1": {"type": "choice", "criteria": ["a", "b"]}}
SLOT, CAP = fm.SLOT_TOKENS, fm.REQUEST_TOKEN_CAP


def bound():
    return fm.request_bound(STATE, Q)


async def ask(mj, ledger=None):
    if ledger is None:
        return await mj.ask(STATE, Q, user_id="k")
    tok = fm.bind(ledger)
    try:
        return await mj.ask(STATE, Q, user_id="k")
    finally:
        fm.unbind(tok)


def test_constants_and_reservation_size():
    assert fm.REQUEST_TOKEN_CAP == 28_000  # 긴 문장 + 문단 8개 실측 25~27k보다 크게(A5)
    assert fm.MAX_ATTEMPTS == 2  # ServiceJevClient는 한 번 다시 보낸다(for attempt in (1, 2))
    assert SLOT == CAP * fm.MAX_ATTEMPTS == 56_000
    assert fm.reservation_for(5) == (5 + fm.TRIAGE_CALLS) * SLOT  # 분류 묶음 1회 포함
    assert fm.reservation_for(1, triage=False) == SLOT
    assert fm.reservation_for(30) == 1_736_000


def test_request_bound_is_utf8_bytes_of_body_plus_overhead():
    body = json.dumps({"model": "jev-1.13.0", "state": STATE, "questions": Q}, ensure_ascii=False)
    assert bound() - len(body.encode("utf-8")) == fm.BOUND_OVERHEAD


def test_no_ledger_means_no_paid_call():
    inner = FakeServiceJev()
    r = asyncio.run(ask(fm.MeteredJev(inner)))
    assert not r.ok and r.error_code == "unmetered" and inner.calls == 0


def test_request_over_cap_is_refused(monkeypatch):
    monkeypatch.setattr(fm, "REQUEST_TOKEN_CAP", bound() - 1)
    inner, led = FakeServiceJev(), fm.Ledger(10 ** 9)
    r = asyncio.run(ask(fm.MeteredJev(inner), led))
    assert not r.ok and r.error_code == "request_too_large" and inner.calls == 0 and led.charged == 0


def test_budget_below_one_slot_is_refused_before_call():
    inner, led = FakeServiceJev(), fm.Ledger(SLOT - 1)
    r = asyncio.run(ask(fm.MeteredJev(inner), led))
    assert not r.ok and r.error_code == "budget" and inner.calls == 0 and led.charged == 0


def test_success_charges_reported_tokens_and_refunds_rest():
    inner, led = FakeServiceJev(tokens=700), fm.Ledger(10 ** 9)
    r = asyncio.run(ask(fm.MeteredJev(inner), led))
    assert r.ok and led.charged == 700 and led.calls == 1


def test_cached_answer_costs_nothing():
    inner, led = FakeServiceJev(cached=True), fm.Ledger(10 ** 9)
    r = asyncio.run(ask(fm.MeteredJev(inner), led))
    assert r.ok and led.charged == 0


def test_failed_attempts_are_unknown_and_charged_at_cap():
    # 1차 실패(보고 없음) + 2차 성공(보고 700): 실패 시도는 청구됐을 수 있다 → 요청 상한으로 센다
    inner, led = FakeServiceJev(tokens=700, attempts=2), fm.Ledger(10 ** 9)
    asyncio.run(ask(fm.MeteredJev(inner), led))
    assert led.charged == 700 + CAP
    inner, led = FakeServiceJev(tokens=0, attempts=2, ok=False), fm.Ledger(10 ** 9)
    asyncio.run(ask(fm.MeteredJev(inner), led))
    assert led.charged == SLOT


@pytest.mark.parametrize("tokens", [-5, "100", None, True, 1.5])
def test_negative_or_abnormal_usage_is_charged_at_slot(tokens):
    inner, led = FakeServiceJev(tokens=tokens), fm.Ledger(10 ** 9)
    asyncio.run(ask(fm.MeteredJev(inner), led))
    assert led.charged == SLOT  # 요청 바이트 추정(bound × 2)이 아니라 호출 몫 그대로(B6)


def test_exception_keeps_full_slot():
    inner, led = FakeServiceJev(exc=RuntimeError("boom")), fm.Ledger(10 ** 9)
    with pytest.raises(RuntimeError):
        asyncio.run(ask(fm.MeteredJev(inner), led))
    assert led.charged == SLOT


def test_cancel_keeps_full_slot():
    async def go():
        inner, led = FakeServiceJev(delay=5), fm.Ledger(10 ** 9)
        t = asyncio.create_task(ask(fm.MeteredJev(inner), led))
        await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        return led.charged

    assert asyncio.run(go()) == SLOT


def test_report_above_cap_is_charged_as_reported():
    inner, led = FakeServiceJev(tokens=CAP * 3), fm.Ledger(10 ** 9)
    asyncio.run(ask(fm.MeteredJev(inner), led))
    assert led.charged == CAP * 3  # 보고된 실제를 줄이지 않는다


def test_no_api_key_result_without_attempts_costs_nothing():
    class NoKey:
        async def ask(self, state, questions, *, user_id, log_ctx=None, usage=None):
            return ServiceJevResult(jev.request_key(state, questions), False, None, 0.0, 0, 0, "FileNotFoundError",
                                    "no_api_key", None)

    led = fm.Ledger(10 ** 9)
    r = asyncio.run(ask(fm.MeteredJev(NoKey()), led))
    assert r.error_code == "no_api_key" and led.charged == 0  # 요청이 나가지 않았다(A4)


def test_closed_ledger_refuses_and_freezes_late_refunds():
    async def go():
        inner, led = FakeServiceJev(tokens=10, delay=0.05), fm.Ledger(10 ** 9)
        mj = fm.MeteredJev(inner)
        t = asyncio.create_task(ask(mj, led))
        await asyncio.sleep(0.01)
        frozen = led.close()
        await t
        late = await ask(mj, led)
        return frozen, led.charged, late, inner.calls

    frozen, after, late, calls = asyncio.run(go())
    assert frozen == SLOT and after == frozen
    assert not late.ok and late.error_code == "unmetered" and calls == 1


def test_ledger_spends_within_budget_only():
    async def go():
        inner, led = FakeServiceJev(delay=0.05, tokens=1), fm.Ledger(SLOT * 2)
        mj = fm.MeteredJev(inner)
        rs = await asyncio.gather(*(ask(mj, led) for _ in range(5)))
        return rs, inner.calls

    rs, calls = asyncio.run(go())
    assert calls == 2 and sum(r.ok for r in rs) == 2


def test_usage_dict_passthrough():
    async def go():
        led, usage = fm.Ledger(10 ** 9), {}
        tok = fm.bind(led)
        try:
            await fm.MeteredJev(FakeServiceJev(tokens=33)).ask(STATE, Q, user_id="k", usage=usage)
        finally:
            fm.unbind(tok)
        return usage

    assert asyncio.run(go())["tokens"] == 33


# ── 실제 ServiceJevClient + 원시 응답 검증(B5·A4) ─────────────────────────────

def _payload(tokens):
    return {"model": jev.MODEL, "usage": {"input_tokens": tokens, "output_tokens": 3},
            "answers": {"p1": {"type": "choice", "probabilities": {"a": 0.6, "b": 0.4}}}}


def _real(handler=None, *, api_key="k"):
    """실제 ServiceJevClient를 계량 래퍼로 만든다(HTTP는 MockTransport, 외부 호출 없음). 요청 수를 센다."""
    seen = []

    def wrap(req):
        seen.append(req)
        return handler(req)

    transport = httpx.MockTransport(wrap) if handler else None
    return fm.service_client(api_key=api_key, transport=transport), seen


@pytest.mark.parametrize("raw", [True, 1.9, "1", -3, None])
def test_real_client_raw_usage_abnormal_is_charged_at_slot(raw):
    async def go():
        mj, seen = _real(lambda req: httpx.Response(200, json=_payload(raw)))
        led = fm.Ledger(10 ** 9)
        try:
            await ask(mj, led)
        finally:
            await mj.inner.aclose()
        return led.charged, len(seen)

    charged, n = asyncio.run(go())
    assert n >= 1 and charged == SLOT  # int()로 1이 되기 전 원시 값으로 본다


def test_real_client_valid_usage_is_charged_exactly():
    async def go():
        mj, seen = _real(lambda req: httpx.Response(200, json=_payload(1234)))
        led = fm.Ledger(10 ** 9)
        try:
            r = await ask(mj, led)
        finally:
            await mj.inner.aclose()
        return r.ok, led.charged, len(seen)

    assert asyncio.run(go()) == (True, 1234, 1)


def test_real_client_5xx_then_ok_charges_cap_for_failed_attempt():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, json={"error": "x"}) if len(calls) == 1 else httpx.Response(200, json=_payload(900))

    async def go():
        mj, _ = _real(handler)
        led = fm.Ledger(10 ** 9)
        try:
            await ask(mj, led)
        finally:
            await mj.inner.aclose()
        return led.charged

    assert asyncio.run(go()) == 900 + CAP


def test_real_client_without_api_key_sends_nothing_and_costs_nothing():
    def no_key():
        raise FileNotFoundError("no key")

    async def go():
        mj, seen = _real(lambda req: httpx.Response(200, json=_payload(1)), api_key=no_key)
        led = fm.Ledger(10 ** 9)
        try:
            r = await ask(mj, led)
        finally:
            await mj.inner.aclose()
        return r.error_code, led.charged, len(seen)

    assert asyncio.run(go()) == ("no_api_key", 0, 0)


# ── #61 교차 검수: 0토큰 환급·확인된 사용량보다 적은 정산 ─────────────────────

def test_real_client_success_reporting_zero_tokens_is_not_free():
    """요청이 나갔는데 usage.input_tokens=0이면 그 시도는 비정상(요청 상한) — 0 환급 금지(#61 검수 1)."""
    async def go():
        mj, seen = _real(lambda req: httpx.Response(200, json=_payload(0)))
        led = fm.Ledger(10 ** 9)
        try:
            r = await ask(mj, led)
        finally:
            await mj.inner.aclose()
        return r.ok, led.charged, len(seen)

    ok, charged, n = asyncio.run(go())
    assert ok and n == 1 and charged == CAP


@pytest.mark.parametrize("raw,expected", [
    ([None, 84_000], 84_000),  # 비정상이 섞여도 확인된 큰 사용량보다 적게 정산하지 않는다
    ([None, 10], SLOT),  # 확인된 합이 작으면 호출 몫
    (["1", 70_000], 70_000),
    ([True], SLOT),
])
def test_abnormal_mixed_with_known_usage_never_charges_below_known(raw, expected, caplog):
    """비정상 값이 섞이면 max(호출 몫, 확인된 합 + 값 없는 시도 × 상한)(#61 검수 2)."""
    import logging

    caplog.set_level(logging.ERROR)
    assert fm.charge_from_record(fm.CallRecord(sent=len(raw), raw=list(raw))) == expected
    if expected > SLOT:
        assert "token_cap_exceeded" in caplog.text  # 상한을 넘는 확인된 사용량은 이 경로에서도 로그


def test_known_usage_with_missing_attempt_charges_cap_for_missing():
    assert fm.charge_from_record(fm.CallRecord(sent=2, raw=[fm._MISSING, 900])) == 900 + CAP
    assert fm.charge_from_record(fm.CallRecord(sent=2, raw=[fm._MISSING, 0])) == 2 * CAP  # 0 보고도 모르는 시도


# ── T4-B4: 401 연속이면 차단, 시험 호출로 다시 연다 ─────────────────────────

class _Status:
    """http_status만 바꿔 돌려주는 ServiceJevClient 대역(요청 수를 센다). statuses 끝 값은 계속 쓴다."""

    def __init__(self, statuses):
        self.statuses, self.calls = list(statuses), 0

    async def ask(self, state, questions, *, user_id, log_ctx=None, usage=None):
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        ok = status == 200
        if usage is not None:
            usage["calls"] = usage.get("calls", 0) + 1
            usage["tokens"] = usage.get("tokens", 0) + (100 if ok else 0)
        answers = {q: {"a": 0.6, "b": 0.4} for q in questions} if ok else None
        return ServiceJevResult(jev.request_key(state, questions), ok, answers, 1.0, 100 if ok else 0, 1,
                                None if ok else f"HTTP {status}", None if ok else "http_4xx", status)


def _run_many(mj, led, n):
    async def go():
        return [await ask(mj, led) for _ in range(n)]
    return asyncio.run(go())


def test_three_consecutive_401_trip_breaker():
    fired = []
    inner = _Status([401, 401, 401, 200])
    mj = fm.MeteredJev(inner, on_auth_block=lambda: fired.append(1))
    led = fm.Ledger(10 ** 9)
    rs = _run_many(mj, led, 5)
    assert inner.calls == 3 and fired == [1] and mj.blocked  # 세 번째에서 끊고, 그 뒤로는 보내지 않는다
    assert [r.error_code for r in rs[3:]] == ["no_api_key", "no_api_key"]
    assert led.charged == 3 * CAP  # 막힌 뒤 호출은 한도를 쓰지 않는다


def test_403_is_logged_but_never_trips(caplog):
    """403은 WAF처럼 문장 내용으로도 날 수 있어 세지 않는다(익명 사용자가 데모를 닫지 못하게). 로그만 남긴다."""
    import logging

    caplog.set_level(logging.WARNING)
    inner = _Status([403])
    mj = fm.MeteredJev(inner, on_auth_block=lambda: None)
    _run_many(mj, fm.Ledger(10 ** 9), 6)
    assert not mj.blocked and inner.calls == 6
    assert "factcheck_auth_forbidden" in caplog.text


def test_403_between_401s_neither_counts_nor_resets():
    inner = _Status([401, 403, 401, 403, 401])
    mj = fm.MeteredJev(inner, on_auth_block=lambda: None)
    _run_many(mj, fm.Ledger(10 ** 9), 5)
    assert mj.blocked  # 401이 세 번(403은 끼어도 지우지 않는다)


def test_success_between_rejections_resets_breaker():
    inner = _Status([401, 401, 200, 401, 401, 200])
    mj = fm.MeteredJev(inner, on_auth_block=lambda: None)
    _run_many(mj, fm.Ledger(10 ** 9), 6)
    assert not mj.blocked and inner.calls == 6


def test_probe_reopens_breaker_on_success_inside_ledger(caplog):
    import logging

    caplog.set_level(logging.INFO)
    events = []
    inner = _Status([401, 401, 401])
    mj = fm.MeteredJev(inner, on_auth_block=lambda: events.append("block"),
                       on_auth_unblock=lambda: events.append("unblock"))
    led = fm.Ledger(10 ** 9)
    _run_many(mj, led, 3)
    assert mj.blocked
    inner.statuses = [200]  # 키를 바꿨다

    async def probe():
        tok = fm.bind(led)
        try:
            return await mj.probe(user_id="auth-probe")
        finally:
            fm.unbind(tok)

    assert asyncio.run(probe()) is True
    assert not mj.blocked and events == ["block", "unblock"]
    assert led.charged == 3 * CAP + 100  # 시험 호출도 원장 안에서 센다
    assert "factcheck_auth_blocked" in caplog.text and "factcheck_auth_unblocked" in caplog.text
    assert _run_many(mj, led, 1)[0].ok  # 다시 보낸다


def test_probe_still_rejected_keeps_breaker_closed():
    inner = _Status([401])
    mj = fm.MeteredJev(inner, on_auth_block=lambda: None)
    led = fm.Ledger(10 ** 9)
    _run_many(mj, led, 3)

    async def probe():
        tok = fm.bind(led)
        try:
            return await mj.probe(user_id="auth-probe")
        finally:
            fm.unbind(tok)

    assert asyncio.run(probe()) is False and mj.blocked and inner.calls == 4


def test_probe_without_ledger_sends_nothing():
    inner = _Status([401])
    mj = fm.MeteredJev(inner)
    _run_many(mj, fm.Ledger(10 ** 9), 3)
    assert asyncio.run(mj.probe(user_id="auth-probe")) is False and inner.calls == 3


def test_real_client_401_propagates_status_and_trips():
    fired = []

    async def go():
        mj = fm.service_client(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(401)),
                               on_auth_block=lambda: fired.append(1))
        led = fm.Ledger(10 ** 9)
        try:
            for _ in range(4):
                await ask(mj, led)
        finally:
            await mj.inner.aclose()
        return mj.blocked

    assert asyncio.run(go()) is True and fired == [1]
