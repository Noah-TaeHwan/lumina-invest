# tests/factcheck/test_fc_metering.py
"""팩트체커 JEV 계량(비용 상한 1·2·5): 호출별 사용량 원장과 요청당 상한.

- 원장(Ledger) 없이는 유료 호출이 나가지 않는다(계량 안 된 경로 없음).
- 요청 상한 = 요청 본문 UTF-8 바이트(토큰 ≤ 바이트 가정) — REQUEST_TOKEN_CAP을 넘는 요청은 보내지 않는다.
- 호출 전에 상한 × 재시도 포함 최대 시도 수를 원장 예산에서 잡고, 끝나면 보고된 사용량으로 바꾼다.
  실패한 시도·취소·예외·음수/비정상 보고는 모르는 사용량이라 크게(상한) 잡는다.
- 원장을 닫은 뒤에는 새 호출도 환급도 없다(정산 뒤 늦게 끝난 호출이 숫자를 바꾸지 않는다).
DB가 필요 없다.
"""
import asyncio
import json

import pytest

from app.services.factcheck import metering as fm
from tests.factcheck.fc_support import FakeServiceJev

STATE, Q = "회사: 삼성전자\n주장: 2025년 매출은 300조원이다.", {"p1": {"type": "choice", "criteria": ["a", "b"]}}


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
    assert fm.MAX_ATTEMPTS == 2  # ServiceJevClient는 한 번 다시 보낸다(for attempt in (1, 2))
    assert fm.SLOT_TOKENS == fm.REQUEST_TOKEN_CAP * fm.MAX_ATTEMPTS
    assert fm.reservation_for(5) == (5 + fm.TRIAGE_CALLS) * fm.SLOT_TOKENS  # 분류 묶음 1회 포함
    assert fm.reservation_for(1, triage=False) == fm.SLOT_TOKENS
    assert fm.reservation_for(0) == fm.TRIAGE_CALLS * fm.SLOT_TOKENS


def test_request_bound_is_utf8_bytes_of_body_plus_overhead():
    body = json.dumps({"model": "jev-1.13.0", "state": STATE, "questions": Q}, ensure_ascii=False)
    assert bound() >= len(body.encode("utf-8"))
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


def test_budget_exhausted_is_refused_before_call():
    inner, led = FakeServiceJev(), fm.Ledger(bound() * fm.MAX_ATTEMPTS - 1)
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


def test_failed_attempts_are_unknown_and_charged_at_bound():
    # 1차 실패(시간 초과 등, 보고 0) + 2차 성공(보고 700): 실패 시도는 청구됐을 수 있다 → 상한으로 센다
    inner, led = FakeServiceJev(tokens=700, attempts=2), fm.Ledger(10 ** 9)
    asyncio.run(ask(fm.MeteredJev(inner), led))
    assert led.charged == min(700 + bound(), bound() * fm.MAX_ATTEMPTS)
    inner, led = FakeServiceJev(tokens=0, attempts=2, ok=False), fm.Ledger(10 ** 9)
    asyncio.run(ask(fm.MeteredJev(inner), led))
    assert led.charged == bound() * fm.MAX_ATTEMPTS


@pytest.mark.parametrize("tokens", [-5, "100", None, True, 1.5])
def test_negative_or_abnormal_usage_is_charged_at_slot(tokens):
    inner, led = FakeServiceJev(tokens=tokens), fm.Ledger(10 ** 9)
    asyncio.run(ask(fm.MeteredJev(inner), led))
    assert led.charged == bound() * fm.MAX_ATTEMPTS


def test_exception_keeps_full_slot():
    inner, led = FakeServiceJev(exc=RuntimeError("boom")), fm.Ledger(10 ** 9)
    with pytest.raises(RuntimeError):
        asyncio.run(ask(fm.MeteredJev(inner), led))
    assert led.charged == bound() * fm.MAX_ATTEMPTS


def test_cancel_keeps_full_slot():
    async def go():
        inner, led = FakeServiceJev(delay=5), fm.Ledger(10 ** 9)
        t = asyncio.create_task(ask(fm.MeteredJev(inner), led))
        await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        return led.charged

    assert asyncio.run(go()) == bound() * fm.MAX_ATTEMPTS


def test_report_above_bound_is_charged_as_reported():
    inner, led = FakeServiceJev(tokens=bound() * 5), fm.Ledger(10 ** 9)
    asyncio.run(ask(fm.MeteredJev(inner), led))
    assert led.charged == bound() * 5  # 가정이 깨져도 보고된 실제를 줄이지 않는다


def test_closed_ledger_refuses_and_freezes_late_refunds():
    async def go():
        inner, led = FakeServiceJev(tokens=10, delay=0.05), fm.Ledger(10 ** 9)
        mj = fm.MeteredJev(inner)
        t = asyncio.create_task(ask(mj, led))
        await asyncio.sleep(0.01)
        frozen = led.close()  # 정산: 진행 중 호출은 상한 그대로
        await t
        late = await ask(mj, led)
        return frozen, led.charged, late, inner.calls

    frozen, after, late, calls = asyncio.run(go())
    assert frozen == bound() * fm.MAX_ATTEMPTS and after == frozen
    assert not late.ok and late.error_code == "unmetered" and calls == 1


def test_ledger_spends_within_budget_only():
    async def go():
        slot = bound() * fm.MAX_ATTEMPTS
        inner, led = FakeServiceJev(delay=0.05, tokens=1), fm.Ledger(slot * 2)
        mj = fm.MeteredJev(inner)
        rs = await asyncio.gather(*(ask(mj, led) for _ in range(5)))
        return rs, inner.calls

    rs, calls = asyncio.run(go())
    assert calls == 2 and sum(r.ok for r in rs) == 2  # 동시 5건 중 예산 두 몫만 나간다


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
