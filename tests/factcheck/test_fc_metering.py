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
