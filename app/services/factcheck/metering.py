# app/services/factcheck/metering.py
"""팩트체커 유료 JEV 호출 계량: 요청당 입력 토큰 상한과 검수 실행별 사용량 원장(검수 결과 비용 상한 1·2·5).

원칙: 어떤 경로로도 하루 상한을 넘는 유료 호출이 나가면 안 되고, 모르는 사용량은 크게 잡는다.

- 요청 상한: JEV 입력 토큰 수는 요청 본문({model, state, questions}) UTF-8 바이트 수를 넘지 않는다고 본다(바이트 단위
  토크나이저 가정 — 로컬에서 실제 호출로 확인할 항목). 여기에 고정 여유(BOUND_OVERHEAD)를 더한 값이 그 요청의 상한이다.
  기존 JEV 클라이언트(app/lib/jev.py·jev_service.py)에는 요청당 상한이 없어 이 모듈이 정하고 강제한다:
  상한이 REQUEST_TOKEN_CAP을 넘는 요청은 보내지 않는다(error_code 'request_too_large' → 파이프라인이 ⊘로 보인다).
  24,000은 문장 하나(최대 2,000자 × 3바이트) + 문단 8개(문단 600자 × 3바이트, evidence.passages.MAX_CHARS) + 질문·틀
  여유로 잡았다.
- 예약: 문장마다 REQUEST_TOKEN_CAP × MAX_ATTEMPTS(ServiceJevClient는 실패하면 한 번 다시 보낸다) = SLOT_TOKENS.
  검수 실행 하나 = (입력 전체 문장 수 + 분류 묶음 1회) × SLOT_TOKENS(비주장 문장도 빼지 않는다: 분류 실패 시 전부 검수).
- 원장(Ledger): 검수 실행마다 하나, 예산 = 그 실행의 예약량. 호출 전에 그 요청의 상한 × MAX_ATTEMPTS를 원장에서 잡고
  (예산을 넘으면 보내지 않는다), 끝나면 보고된 사용량으로 바꾼다. 실패한 시도는 청구됐을 수 있어 상한으로 센다.
  예외·취소·음수/정수 아님/성공인데 0 같은 비정상 보고는 잡아 둔 몫을 그대로 둔다. 보고가 상한보다 크면(가정이 깨짐)
  보고값을 그대로 쓰고 오류 로그를 남긴다.
- 원장은 ContextVar로 검수 작업에 묶는다(bind). 파이프라인이 안에서 만드는 작업은 컨텍스트를 물려받는다.
  원장 없이 부르면 보내지 않는다(error_code 'unmetered') — 계량 안 된 유료 호출 경로가 없다.
- 원장을 닫으면(close, 정산 직전) 새 호출을 받지 않고 늦게 끝난 호출의 환급도 무시한다(진행 중 호출은 상한 그대로 정산).
- 로그에는 state·주장·문단 원문을 남기지 않는다.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from typing import Any

from app.lib import jev
from app.lib.jev_service import ServiceJevResult

REQUEST_TOKEN_CAP = 24_000
MAX_ATTEMPTS = 2
SLOT_TOKENS = REQUEST_TOKEN_CAP * MAX_ATTEMPTS
TRIAGE_CALLS = 1
BOUND_OVERHEAD = 256  # HTTP 본문 밖 모델 측 틀·특수 토큰 여유

log = logging.getLogger("app.factcheck.metering")

_LEDGER: contextvars.ContextVar["Ledger | None"] = contextvars.ContextVar("factcheck_ledger", default=None)


def request_bound(state: str, questions: dict) -> int:
    """요청 하나의 입력 토큰 상한: 본문 UTF-8 바이트 + 고정 여유."""
    body = json.dumps({"model": jev.MODEL, "state": state, "questions": questions}, ensure_ascii=False)
    return len(body.encode("utf-8")) + BOUND_OVERHEAD


def reservation_for(n_sentences: int, *, triage: bool = True) -> int:
    """검수 실행 하나의 예약량: (문장 수 + 분류 묶음 호출) × 요청 상한 × 최대 시도."""
    return (max(0, n_sentences) + (TRIAGE_CALLS if triage else 0)) * REQUEST_TOKEN_CAP * MAX_ATTEMPTS


class Ledger:
    """검수 실행 하나의 호출별 사용량 원장. charged = 정산할 토큰(진행 중 호출은 상한으로 들어 있다)."""

    def __init__(self, budget: int):
        self.budget = int(budget)
        self.charged = 0
        self.calls = 0
        self.refused = 0
        self.closed = False
        self.settlement: asyncio.Future | None = None  # 정산 작업(라우트가 한 번만 만든다)

    def close(self) -> int:
        """새 호출·환급을 막고 정산할 값을 돌려준다."""
        self.closed = True
        return self.charged


def bind(ledger: Ledger) -> contextvars.Token:
    """현재 작업(과 그 안에서 만드는 작업)에 원장을 묶는다."""
    return _LEDGER.set(ledger)


def unbind(token: contextvars.Token) -> None:
    """bind를 되돌린다."""
    _LEDGER.reset(token)


def _refused(state: str, questions: dict, code: str) -> ServiceJevResult:
    return ServiceJevResult(jev.request_key(state, questions), False, None, 0.0, 0, 0, "refused", code, None)


def _valid_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def charge_for(result: Any, usage: dict, bound: int) -> int:
    """끝난 호출 하나의 청구 토큰. 모르는 부분은 상한으로 센다."""
    slot = bound * MAX_ATTEMPTS
    reported, rt, attempts = usage.get("tokens"), getattr(result, "input_tokens", None), getattr(result, "attempts", None)
    if not (_valid_int(reported) and _valid_int(rt) and reported == rt and _valid_int(attempts)
            and attempts <= MAX_ATTEMPTS):
        return slot
    ok, cached = bool(getattr(result, "ok", False)), bool(getattr(result, "cached", False))
    if cached:
        return 0 if (attempts == 0 and reported == 0) else slot
    if attempts == 0 or (ok and reported == 0):
        return slot  # 나간 호출인데 시도·토큰 보고가 없다
    failed = attempts - (1 if ok else 0)
    charge = reported + bound * failed
    if reported > bound * attempts:
        log.error(json.dumps({"event": "token_bound_exceeded", "reported": reported, "bound": bound}))
        return charge
    return min(charge, slot)


class MeteredJev:
    """ServiceJevClient(또는 같은 ask 모양)를 감싸 원장 안에서만 유료 호출을 보낸다. 프로세스에 하나 두고 파이프라인에 넣는다."""

    def __init__(self, inner: Any):
        self.inner = inner

    async def ask(self, state: str, questions: dict, *, user_id: str, log_ctx: dict | None = None,
                  usage: dict | None = None) -> ServiceJevResult:
        """원장 예산 안에서만 보낸다. 거부하면 ok=False 결과(error_code: unmetered / request_too_large / budget)."""
        ledger = _LEDGER.get()
        if ledger is None or ledger.closed:
            return _refused(state, questions, "unmetered")
        bound = request_bound(state, questions)
        if bound > REQUEST_TOKEN_CAP:
            ledger.refused += 1
            return _refused(state, questions, "request_too_large")
        slot = bound * MAX_ATTEMPTS
        if ledger.charged + slot > ledger.budget:
            ledger.refused += 1
            return _refused(state, questions, "budget")
        ledger.charged += slot  # 보내기 전에 잡는다(동시 호출도 예산을 넘지 못한다)
        ledger.calls += 1
        inner_usage: dict = {}
        result = await self.inner.ask(state, questions, user_id=user_id, log_ctx=log_ctx, usage=inner_usage)
        # 예외·취소는 여기 오지 않는다: 잡아 둔 몫(상한)이 그대로 남는다
        if not ledger.closed:
            ledger.charged += charge_for(result, inner_usage, bound) - slot
        if usage is not None:
            for k in ("calls", "tokens"):
                usage[k] = usage.get(k, 0) + (inner_usage.get(k, 0) if isinstance(inner_usage.get(k, 0), int) else 0)
        return result
