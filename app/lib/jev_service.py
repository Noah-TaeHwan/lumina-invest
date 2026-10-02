# app/lib/jev_service.py
"""제품(채팅 근거 판정)용 비동기 TypeSafe JEV 클라이언트와 Redis 일일 한도(A-2 spec 4-3·7절).

- 평가용 JevClient(app/lib/jev.py)는 고치지 않는다. 요청 키(jev.request_key)·응답 검증(jev.parse_answers)·모델(jev.MODEL)을
  그대로 써서 같은 요청이면 판정 결과가 비트 단위로 같게 한다. 재시도 루프만 이 파일에 따로 있다.
- 시도당 타임아웃 2초, 재시도 1회(지연 없음). 429·401·403은 재시도하지 않는다.
- 요청이 나간 뒤 실행 마감으로 취소되면 응답 토큰을 알 수 없으므로 호출 1회 + 추정 토큰(EST_TOKENS_PER_CALL)을 센다.
- 성공 응답만 Redis에 30일 캐시한다(키 evidence:jev:{request_key}). 캐시 Redis 오류는 캐시 없음으로 넘긴다.
- 한도는 Redis 카운터로 센다: 실행 시작 전 잔여 확인(Quota.check) → 시도마다 실제 usage.input_tokens와 호출 1회를 INCRBY.
  날짜는 KST. 제품 호출은 평가 원장(lab/data/evidence/jev_calls.jsonl)에 쓰지 않는다.
- redis 패키지를 import하지 않는다(get·set·incrby·expire를 가진 비동기 클라이언트면 된다).
- 로그에는 state·주장·문단 원문, API 키를 남기지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from app.lib import jev

TIMEOUT_S = 2.0
CACHE_PREFIX = "evidence:jev:"
CACHE_TTL_S = 30 * 86400
QUOTA_PREFIX = "evidence:quota"
QUOTA_TTL_S = 48 * 3600
EST_TOKENS_PER_CALL = 5070  # 호출당 입력 토큰 p95(spec 2.3절)
MAX_PROCESS_CONCURRENCY = 6
KST = timezone(timedelta(hours=9))
NO_RETRY_STATUS = frozenset({401, 403, 429})

log = logging.getLogger("app.evidence.jev")


class QuotaUnavailable(RuntimeError):
    """Redis를 읽거나 쓸 수 없어 한도를 셀 수 없다."""


@dataclass(frozen=True)
class QuotaLimits:
    """일일 한도(spec 7.2절). 금액이 아니라 호출 수·입력 토큰 수로만 정한다."""

    user_calls: int = 150
    user_tokens: int = 750_000
    global_tokens: int = 3_000_000


class Quota:
    """Redis 일일 카운터. 예약·정산 없이 시작 전 확인과 호출 뒤 가산만 한다."""

    def __init__(self, redis: Any, limits: QuotaLimits = QuotaLimits(),
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._r = redis
        self.limits = limits
        self._now = now

    def day(self) -> str:
        return self._now().astimezone(KST).strftime("%Y%m%d")

    def keys(self, user_id: str) -> tuple[str, str, str]:
        """(사용자 호출 수, 사용자 입력 토큰, 전역 입력 토큰) 키."""
        d = f"{QUOTA_PREFIX}:{self.day()}"
        return f"{d}:user:{user_id}:calls", f"{d}:user:{user_id}:tokens", f"{d}:global:tokens"

    async def check(self, user_id: str, n_calls: int) -> str | None:
        """판정할 주장 n개(호출 n회 × 토큰 p95)를 감당할 잔여가 없으면 'cap_user'/'cap_global', 있으면 None."""
        try:
            calls, tokens, glob = [int(await self._r.get(k) or 0) for k in self.keys(user_id)]
        except Exception as exc:  # noqa: BLE001 — 어떤 Redis 오류든 한도를 셀 수 없다는 뜻이다
            raise QuotaUnavailable(type(exc).__name__) from exc
        need = n_calls * EST_TOKENS_PER_CALL
        if calls + n_calls > self.limits.user_calls or tokens + need > self.limits.user_tokens:
            return "cap_user"
        if glob + need > self.limits.global_tokens:
            return "cap_global"
        return None

    async def record(self, user_id: str, input_tokens: int) -> None:
        """호출 1회와 실제 입력 토큰을 더한다."""
        k_calls, k_tokens, k_glob = self.keys(user_id)
        try:
            for key, amount in ((k_calls, 1), (k_tokens, input_tokens), (k_glob, input_tokens)):
                await self._r.incrby(key, amount)
                await self._r.expire(key, QUOTA_TTL_S)
        except Exception as exc:  # noqa: BLE001
            raise QuotaUnavailable(type(exc).__name__) from exc


@dataclass
class ServiceJevResult:
    """요청 하나(재시도 포함)의 결과. ok=False면 answers는 None이다. input_tokens는 모든 시도의 합이다."""

    key: str
    ok: bool
    answers: dict[str, dict[str, float]] | None
    latency_ms: float
    input_tokens: int
    attempts: int
    error: str | None
    error_code: str | None  # timeout / http_5xx / http_429 / http_4xx / invalid_response / no_api_key
    http_status: int | None
    cached: bool = False


class ServiceJevClient:
    """비동기 JEV 클라이언트. 프로세스에 하나 두고 실행기들이 같이 쓴다(전체 동시 호출 상한)."""

    def __init__(self, redis: Any, quota: Quota, *, api_key: str | Callable[[], str] = jev.load_api_key,
                 client: httpx.AsyncClient | None = None, timeout: float = TIMEOUT_S):
        self._r = redis
        self.quota = quota
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout
        self._sem = asyncio.Semaphore(MAX_PROCESS_CONCURRENCY)

    async def _cache_get(self, key: str) -> dict | None:
        try:
            raw = await self._r.get(CACHE_PREFIX + key)
            return json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001 — 캐시는 없어도 된다
            return None

    async def _cache_set(self, key: str, answers: dict) -> None:
        try:
            await self._r.set(CACHE_PREFIX + key, json.dumps(answers, ensure_ascii=False), ex=CACHE_TTL_S)
        except Exception:  # noqa: BLE001
            log.warning(json.dumps({"event": "cache_set_failed", "request_key": key[:12]}))

    def _key_value(self) -> str:
        return self._api_key() if callable(self._api_key) else self._api_key

    async def ask(self, state: str, questions: dict, *, user_id: str, log_ctx: dict | None = None,
                  usage: dict | None = None) -> ServiceJevResult:
        """질문 묶음을 보낸다. 실패하면(429·401·403 제외) 한 번 다시 보내고, 그래도 실패면 ok=False.

        usage를 넘기면 나간 시도 수(calls)와 센 토큰(tokens)을 시도마다 채운다. 취소돼 결과를 못 받는 호출자도
        이 값으로 사용량을 집계할 수 있다.
        """
        key = jev.request_key(state, questions)
        usage = usage if usage is not None else {}
        usage.setdefault("calls", 0)
        usage.setdefault("tokens", 0)
        hit = await self._cache_get(key)
        if hit is not None:
            return ServiceJevResult(key, True, hit, 0.0, 0, 0, None, None, None, cached=True)
        try:
            api_key = self._key_value()
        except Exception as exc:  # noqa: BLE001
            return ServiceJevResult(key, False, None, 0.0, 0, 0, type(exc).__name__, "no_api_key", None)
        body = {"model": jev.MODEL, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {api_key}"}
        total_tokens, latency = 0, 0.0
        error = code = status = None
        attempt = 0
        for attempt in (1, 2):
            answers, tokens, error, code, status = None, 0, None, None, None
            t0, sent = 0.0, False
            try:
                async with self._sem:
                    t0 = time.perf_counter()  # 전역 동시성 대기는 지연에 넣지 않는다
                    sent = True
                    resp = await asyncio.wait_for(self._client.post(jev.API_URL, json=body, headers=headers),
                                                  self._timeout)
                status = resp.status_code
                resp.raise_for_status()
                payload = resp.json()
                tokens = int(payload["usage"]["input_tokens"])  # JevClient처럼 검증 전에 읽어 검증 실패도 센다
                answers = jev.parse_answers(payload, questions)
            except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
                error, code = f"{type(exc).__name__}: timeout", "timeout"
            except asyncio.CancelledError:
                if sent:  # 요청은 이미 나갔다
                    usage["calls"] += 1
                    usage["tokens"] += EST_TOKENS_PER_CALL
                    await self._record(user_id, EST_TOKENS_PER_CALL)
                raise
            except httpx.HTTPStatusError:
                error = f"HTTP {status}"
                code = "http_5xx" if status >= 500 else "http_429" if status == 429 else "http_4xx"
            except httpx.HTTPError as exc:
                error, code = f"{type(exc).__name__}: {str(exc)[:200]}", "http_5xx"
            except (KeyError, TypeError, ValueError) as exc:
                error, code = f"{type(exc).__name__}: {str(exc)[:200]}", "invalid_response"
            latency = (time.perf_counter() - t0) * 1000
            total_tokens += tokens
            usage["calls"] += 1
            usage["tokens"] += tokens
            await self._record(user_id, tokens)
            log.info(json.dumps({**(log_ctx or {}), "request_key": key[:12], "attempt": attempt, "ok": error is None,
                                 "http_status": status, "latency_ms": round(latency, 1), "input_tokens": tokens,
                                 "error": error[:200] if error else None}, ensure_ascii=False))
            if error is None:
                await self._cache_set(key, answers)
                return ServiceJevResult(key, True, answers, latency, total_tokens, attempt, None, None, status)
            if status in NO_RETRY_STATUS:
                break
        return ServiceJevResult(key, False, None, latency, total_tokens, attempt, error, code, status)

    async def _record(self, user_id: str, tokens: int) -> None:
        """한도 가산. 호출은 이미 나갔으므로 Redis 오류는 기록만 하고 판정은 이어 간다."""
        try:
            await self.quota.record(user_id, tokens)
        except QuotaUnavailable as exc:
            log.error(json.dumps({"event": "quota_record_failed", "error": str(exc)}))
