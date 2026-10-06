# app/services/factcheck/metering.py
"""팩트체커 유료 JEV 호출 계량: 요청당 입력 토큰 상한과 검수 실행별 사용량 원장(PR #57 비용 상한, 배포 전 검수 A4·A5·B5·B6).

원칙: 어떤 경로로도 하루 상한을 넘는 유료 호출이 나가면 안 되고, 모르는 사용량은 크게 잡는다.

- 요청 상한 REQUEST_TOKEN_CAP(28,000): 기존 JEV 클라이언트(app/lib/jev.py·jev_service.py)에는 요청당 상한이 없어 이 모듈이
  정하고 강제한다. 요청 본문({model, state, questions}) UTF-8 바이트 + 고정 여유(BOUND_OVERHEAD)가 이 값을 넘으면 보내지
  않는다(error_code 'request_too_large' → 파이프라인이 ⊘로 보인다). 긴 문장 + 문단 8개 실측 25~27k보다 크게 잡았다.
- 호출 몫 SLOT_TOKENS = REQUEST_TOKEN_CAP × MAX_ATTEMPTS(ServiceJevClient는 실패하면 한 번 다시 보낸다).
  검수 실행 예약 = (입력 전체 문장 수 + 분류 묶음 1회) × SLOT_TOKENS(비주장 문장도 빼지 않는다: 분류 실패 시 전부 검수).
- 원장(Ledger): 검수 실행마다 하나, 예산 = 그 실행의 예약량. 호출 전에 한 몫(SLOT_TOKENS)을 원장에서 잡고(예산을 넘으면
  보내지 않는다), 끝나면 아는 사용량으로 바꾼다:
  · 요청이 하나도 나가지 않았다(API 키 없음·캐시) → 0.
  · 시도마다 응답의 원시 usage.input_tokens가 진짜 정수(bool·실수·문자열 아님, 0 이상)면 그 값, 아니면(실패 시도·시간 초과·
    비정상 값) 그 시도는 REQUEST_TOKEN_CAP. ServiceJevClient가 int()로 바꾸기 전 값을 보려고 httpx 이벤트 훅으로 원시
    응답을 기록한다(service_client). 훅 기록이 없는 클라이언트(테스트 대역)는 결과·usage로 같은 규칙을 적용한다.
  · 예외·취소는 잡아 둔 몫 그대로(요청 바이트 추정이 아니라 호출 몫 — 바이트 가정이 확인되기 전까지 추정으로 줄이지 않는다).
  · 보고가 상한보다 크면 보고값 그대로 쓰고 오류 로그를 남긴다.
- 원장은 ContextVar로 검수 작업에 묶는다(bind). 파이프라인이 안에서 만드는 작업은 컨텍스트를 물려받는다.
  원장 없이 부르면 보내지 않는다(error_code 'unmetered') — 계량 안 된 유료 호출 경로가 없다.
- 원장을 닫으면(close, 정산 직전) 새 호출을 받지 않고 늦게 끝난 호출의 환급도 무시한다(진행 중 호출은 몫 그대로 정산).
- 로그에는 state·주장·문단 원문을 남기지 않는다.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from app.lib import jev
from app.lib.jev_service import TIMEOUT_S, ServiceJevClient, ServiceJevResult

REQUEST_TOKEN_CAP = 28_000
MAX_ATTEMPTS = 2
SLOT_TOKENS = REQUEST_TOKEN_CAP * MAX_ATTEMPTS
TRIAGE_CALLS = 1
BOUND_OVERHEAD = 256  # HTTP 본문 밖 모델 측 틀·특수 토큰 여유
AUTH_TRIP_AFTER = 3  # 401이 이만큼 이어지면 키가 막힌 것으로 본다(키 폐기·만료 때 한도만 타는 것을 막는다)
# 403은 세지 않는다: WAF처럼 요청 내용(사용자 문장)으로도 날 수 있어 익명 사용자가 데모를 닫을 수 있다. 로그만 남긴다

log = logging.getLogger("app.factcheck.metering")

_LEDGER: contextvars.ContextVar["Ledger | None"] = contextvars.ContextVar("factcheck_ledger", default=None)
_CALL: contextvars.ContextVar["CallRecord | None"] = contextvars.ContextVar("factcheck_call", default=None)
_MISSING = object()  # 응답에 usage.input_tokens가 없다(실패 응답·본문 오류)


def request_bound(state: str, questions: dict) -> int:
    """요청 하나의 크기 상한(초과 거부용): 본문 UTF-8 바이트 + 고정 여유."""
    body = json.dumps({"model": jev.MODEL, "state": state, "questions": questions}, ensure_ascii=False)
    return len(body.encode("utf-8")) + BOUND_OVERHEAD


def _slot() -> int:
    return REQUEST_TOKEN_CAP * MAX_ATTEMPTS


def reservation_for(n_sentences: int, *, triage: bool = True) -> int:
    """검수 실행 하나의 예약량: (문장 수 + 분류 묶음 호출) × 요청 상한 × 최대 시도."""
    return (max(0, n_sentences) + (TRIAGE_CALLS if triage else 0)) * _slot()


class Ledger:
    """검수 실행 하나의 호출별 사용량 원장. charged = 정산할 토큰(진행 중 호출은 몫으로 들어 있다)."""

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


@dataclass
class CallRecord:
    """유료 호출 하나에서 실제로 나간 HTTP 요청 수와 응답마다의 원시 usage.input_tokens(읽지 못하면 _MISSING)."""

    sent: int = 0
    raw: list = field(default_factory=list)


def bind(ledger: Ledger) -> contextvars.Token:
    """현재 작업(과 그 안에서 만드는 작업)에 원장을 묶는다."""
    return _LEDGER.set(ledger)


def unbind(token: contextvars.Token) -> None:
    """bind를 되돌린다."""
    _LEDGER.reset(token)


def _refused(state: str, questions: dict, code: str) -> ServiceJevResult:
    return ServiceJevResult(jev.request_key(state, questions), False, None, 0.0, 0, 0, "refused", code, None)


def _valid_int(v: Any) -> bool:
    return type(v) is int and v >= 0  # bool·실수·문자열·음수는 아니다


def _over(reported: int) -> None:
    if reported > REQUEST_TOKEN_CAP * MAX_ATTEMPTS:
        log.error(json.dumps({"event": "token_cap_exceeded", "reported": reported, "cap": REQUEST_TOKEN_CAP}))


def charge_from_record(rec: CallRecord) -> int:
    """HTTP 훅 기록으로 청구 토큰을 낸다. 요청이 안 나갔으면 0.
    나간 요청마다: usage 값이 0보다 큰 진짜 정수면 그 값(확인된 사용량), 값이 없거나(실패 응답·시간 초과로 응답 없음) 0을
    보고하면 그 시도는 요청 상한(나간 요청의 0 보고는 믿지 않는다 — 0 환급 금지). 응답에 usage 값이 있는데 진짜 정수가
    아니면(true·1.9·"1"·음수·null) 비정상이라 max(호출 몫, 확인된 합 + 모르는 시도 × 상한) — 확인된 사용량보다 절대 적지 않게."""
    if rec.sent == 0:
        return 0  # 요청이 나가지 않았다(API 키 없음·캐시)
    abnormal = [v for v in rec.raw if v is not _MISSING and not _valid_int(v)]
    known = [v for v in rec.raw if _valid_int(v) and v > 0]
    _over(sum(known))
    unknown = max(0, rec.sent - len(known) - len(abnormal))  # 값 없음·0 보고·응답 없는 시도
    base = sum(known) + REQUEST_TOKEN_CAP * unknown
    return max(_slot(), base) if abnormal else base


def charge_for(result: Any, usage: dict) -> int:
    """훅 기록이 없을 때: 결과·usage로 청구 토큰을 낸다. 모르는 부분은 시도마다 요청 상한, 통째로 모르면 한 몫."""
    slot = _slot()
    reported, rt, attempts = usage.get("tokens"), getattr(result, "input_tokens", None), getattr(result, "attempts", None)
    code = getattr(result, "error_code", None)
    if attempts == 0 and code == "no_api_key":
        return 0  # 키를 읽지 못해 요청이 나가지 않았다
    if not (_valid_int(reported) and _valid_int(rt) and reported == rt and _valid_int(attempts)
            and attempts <= MAX_ATTEMPTS):
        return slot
    ok, cached = bool(getattr(result, "ok", False)), bool(getattr(result, "cached", False))
    if cached:
        return 0 if (attempts == 0 and reported == 0) else slot
    if attempts == 0 or (ok and reported == 0):
        return slot  # 나간 호출인데 시도·토큰 보고가 없다
    failed = attempts - (1 if ok else 0)
    _over(reported)
    charge = reported + REQUEST_TOKEN_CAP * failed
    return charge if reported > slot else min(charge, slot)


async def _on_request(request: httpx.Request) -> None:
    rec = _CALL.get()
    if rec is not None:
        rec.sent += 1


async def _on_response(response: httpx.Response) -> None:
    rec = _CALL.get()
    if rec is None:
        return
    try:
        await response.aread()
        rec.raw.append(json.loads(response.content)["usage"]["input_tokens"])
    except Exception:  # noqa: BLE001 — 실패 응답·본문 오류: 값 없음(그 시도는 요청 상한)
        rec.raw.append(_MISSING)


def service_client(*, api_key: str | Callable[[], str] = jev.load_api_key,
                   transport: httpx.AsyncBaseTransport | None = None,
                   on_auth_block: Callable[[], None] | None = None,
                   on_auth_unblock: Callable[[], None] | None = None) -> "MeteredJev":
    """실제 ServiceJevClient를 계량 래퍼로 감싼다. 원시 응답 기록 훅을 단 httpx 클라이언트를 넣고, Redis 캐시·근거 모드 한도는
    쓰지 않는다(빈 캐시·빈 기록기 — 한도는 factcheck_quota가 한다)."""
    client = httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport,
                               event_hooks={"request": [_on_request], "response": [_on_response]})
    return MeteredJev(ServiceJevClient(_NoCache(), _NoQuota(), api_key=api_key, client=client), audited=True,
                      on_auth_block=on_auth_block, on_auth_unblock=on_auth_unblock)


class _NoCache:
    """ServiceJevClient 캐시 자리: 늘 비어 있다(팩트체커는 Redis를 쓰지 않는다)."""

    async def get(self, key):
        return None

    async def set(self, key, value, ex=None):
        return True


class _NoQuota:
    """ServiceJevClient 한도 기록 자리: 아무것도 하지 않는다(사용량은 원장 → factcheck_quota로 센다)."""

    async def record(self, user_id, tokens):
        return None


class MeteredJev:
    """ServiceJevClient(또는 같은 ask 모양)를 감싸 원장 안에서만 유료 호출을 보낸다. 프로세스에 하나 두고 파이프라인에 넣는다.
    audited=True면 inner의 HTTP 훅 기록(CallRecord)으로 청구한다(service_client가 만든다)."""

    def __init__(self, inner: Any, *, audited: bool = False, on_auth_block: Callable[[], None] | None = None,
                 on_auth_unblock: Callable[[], None] | None = None):
        self.inner = inner
        self.audited = audited
        self.on_auth_block = on_auth_block  # 차단할 때 한 번 부른다(진입점이 검수를 no_api_key로 닫는다)
        self.on_auth_unblock = on_auth_unblock  # 시험 호출이 성공해 다시 열 때 부른다
        self.blocked = False
        self._auth_failures = 0

    def _note_status(self, status: Any) -> None:
        """401이 AUTH_TRIP_AFTER번 이어지면 막는다(403은 세지도 지우지도 않고 로그만). 그 밖의 응답이 오면 센 것을 지운다.
        막힌 뒤에는 probe()가 성공해야 다시 연다."""
        if status == 401:
            self._auth_failures += 1
            if self._auth_failures >= AUTH_TRIP_AFTER and not self.blocked:
                self.blocked = True
                log.error(json.dumps({"event": "factcheck_auth_blocked", "consecutive": self._auth_failures}))
                if self.on_auth_block is not None:
                    self.on_auth_block()
        elif status == 403:
            log.warning(json.dumps({"event": "factcheck_auth_forbidden"}))
        elif status is not None:
            self._auth_failures = 0

    async def probe(self, *, user_id: str) -> bool:
        """막힌 뒤 가벼운 시험 호출 1회(호출부가 묶은 원장 안에서 — 한도 예약·정산을 거친다). 성공 응답이면 다시 열고
        True. 원장이 없거나 거절·실패면 막힌 채 False."""
        from app.services.evidence import judge  # 근거 모드와 같은 요청 모양(import만)

        if _LEDGER.get() is None:
            return False
        state, questions = judge.build_state("확인", "확인", ["확인"]), judge.build_questions(1)
        result = await self._send(state, questions, user_id=user_id, log_ctx={"stage": "auth_probe"}, usage=None)
        if getattr(result, "ok", False):
            self.blocked, self._auth_failures = False, 0
            log.warning(json.dumps({"event": "factcheck_auth_unblocked"}))
            if self.on_auth_unblock is not None:
                self.on_auth_unblock()
            return True
        return False

    async def ask(self, state: str, questions: dict, *, user_id: str, log_ctx: dict | None = None,
                  usage: dict | None = None) -> ServiceJevResult:
        """원장 예산 안에서만 보낸다. 거부하면 ok=False 결과(error_code: unmetered / no_api_key / request_too_large / budget)."""
        if self.blocked and _LEDGER.get() is not None:  # 판정 키가 막혔다: 보내지 않고 한도도 쓰지 않는다
            return _refused(state, questions, "no_api_key")
        return await self._send(state, questions, user_id=user_id, log_ctx=log_ctx, usage=usage)

    async def _send(self, state: str, questions: dict, *, user_id: str, log_ctx: dict | None,
                    usage: dict | None) -> ServiceJevResult:
        """원장 확인·한 몫 잡기 → 보내기 → 정산값 반영. ask와 probe가 함께 쓴다."""
        ledger = _LEDGER.get()
        if ledger is None or ledger.closed:
            return _refused(state, questions, "unmetered")
        if request_bound(state, questions) > REQUEST_TOKEN_CAP:
            ledger.refused += 1
            return _refused(state, questions, "request_too_large")
        slot = _slot()
        if ledger.charged + slot > ledger.budget:
            ledger.refused += 1
            return _refused(state, questions, "budget")
        ledger.charged += slot  # 보내기 전에 잡는다(동시 호출도 예산을 넘지 못한다)
        ledger.calls += 1
        inner_usage: dict = {}
        rec = CallRecord()
        token = _CALL.set(rec)  # wait_for가 만든 작업에도 같은 객체가 보인다(컨텍스트 복사, 객체는 공유)
        try:
            result = await self.inner.ask(state, questions, user_id=user_id, log_ctx=log_ctx, usage=inner_usage)
        finally:
            _CALL.reset(token)
        # 예외·취소는 여기 오지 않는다: 잡아 둔 몫이 그대로 남는다
        self._note_status(getattr(result, "http_status", None))
        if not ledger.closed:
            charge = charge_from_record(rec) if self.audited else charge_for(result, inner_usage)
            ledger.charged += charge - slot
        if usage is not None:
            for k in ("calls", "tokens"):
                v = inner_usage.get(k, 0)
                usage[k] = usage.get(k, 0) + (v if _valid_int(v) else 0)
        return result
