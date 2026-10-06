# app/services/factcheck/quota.py
"""팩트체커 일일 한도: 익명 키별·서버 전체 JEV 토큰을 PostgreSQL에서 원자적으로 예약하고 정산한다.

설계 D8·Outside Voice #5:
- 표 factcheck_quota(key, day, count, used, reserved). key는 익명 키(sha256(IP + 일별 솔트)) 또는 'global'.
- 예약: 한 트랜잭션에서 `UPDATE ... SET reserved = reserved + :est WHERE used + reserved + :est <= :cap RETURNING`을
  키 행 → 전체 행 순서로 한다(항상 같은 순서라 교착이 없다). 어느 하나라도 행이 안 나오면 롤백하고 QuotaExceeded.
  같은 행을 노리는 동시 요청은 행 잠금으로 줄을 서고, 잠금이 풀린 뒤 WHERE를 다시 평가하므로 같은 잔여량을 둘이 통과할 수 없다.
- 예약에 실패하면 호출하지 않는다(호출부 책임: 예약이 성공해야 파이프라인을 시작한다).
- 정산: reserved에서 예약분을 빼고 used에 실제 토큰을 더한다. 실제를 모르면 호출부가 예약 전액을 넘긴다(보수적).
- 익명 하루 실행 3회(count). 건너뛴 문장 수동 검수는 count_run=False로 토큰만 예약한다.
- 날짜는 KST. 지난 날의 익명 행은 예약할 때 지운다(그날 솔트가 사라져 이미 연결할 수 없지만 남길 이유도 없다). 전체 행은 남긴다.
- 상한 값은 금액이 아니라 입력 토큰 수다. 공개 배포의 전체 상한 FACTCHECK_DAILY_GLOBAL_TOKENS는 노아가 공개 전에 정한다.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.lib.jev_service import EST_TOKENS_PER_CALL

KST = timezone(timedelta(hours=9))
GLOBAL_KEY = "global"
MAX_TARGETS = 30  # 익명 1회 검수 대상 문장 상한(라우트의 422 기준과 같다)


def estimate(n_calls: int) -> int:
    """JEV 호출 n회의 예약 토큰: 호출당 입력 토큰 p95(근거 모드 제품 경로와 같은 값) × n."""
    return max(0, n_calls) * EST_TOKENS_PER_CALL


class QuotaSettings(BaseSettings):
    """팩트체커 한도 설정(환경변수). 앱 공용 Settings(app/config.py)를 고치지 않으려고 따로 둔다."""

    model_config = SettingsConfigDict(env_file=os.getenv("ENV_FILE", ".env.dev"), extra="ignore")  # app/config.py와 같은 파일

    FACTCHECK_DAILY_ANON_RUNS: int = 3
    # 익명 키 하루 토큰: 3회 × 30문장 × 호출당 p95
    FACTCHECK_DAILY_KEY_TOKENS: int = 3 * MAX_TARGETS * EST_TOKENS_PER_CALL
    # 서버 전체 하루 토큰. 보수적 기본값(30문장 검수 약 3회분). 공개 값은 노아가 정한다
    FACTCHECK_DAILY_GLOBAL_TOKENS: int = 500_000


@dataclass(frozen=True)
class QuotaLimits:
    """하루 상한: 익명 키 실행 횟수·익명 키 토큰·서버 전체 토큰."""

    runs: int = 3
    key_tokens: int = 3 * MAX_TARGETS * EST_TOKENS_PER_CALL
    global_tokens: int = 500_000


def limits_from_env() -> QuotaLimits:
    """환경변수(FACTCHECK_DAILY_*)에서 상한을 읽는다."""
    s = QuotaSettings()
    return QuotaLimits(runs=s.FACTCHECK_DAILY_ANON_RUNS, key_tokens=s.FACTCHECK_DAILY_KEY_TOKENS,
                       global_tokens=s.FACTCHECK_DAILY_GLOBAL_TOKENS)


@dataclass(frozen=True)
class Reservation:
    """예약 하나. 정산할 때 같은 key·day·est를 쓴다."""

    key: str
    day: str
    est: int


class QuotaExceeded(Exception):
    """예약 실패. code ∈ {cap_runs, cap_key_tokens, cap_global}."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _kst_day(now: datetime) -> str:
    return now.astimezone(KST).strftime("%Y%m%d")


_ENSURE = text("INSERT INTO factcheck_quota (key, day) VALUES (:k, :d), (:g, :d) ON CONFLICT DO NOTHING")
_PURGE = text("DELETE FROM factcheck_quota WHERE key <> :g AND day < :d")
_TAKE_KEY = text(
    "UPDATE factcheck_quota SET count = count + :inc, reserved = reserved + :est, updated_at = now() "
    "WHERE key = :k AND day = :d AND (:inc = 0 OR count < :runs) AND used + reserved + :est <= :cap "
    "RETURNING count")
_TAKE_GLOBAL = text(
    "UPDATE factcheck_quota SET count = count + :inc, reserved = reserved + :est, updated_at = now() "
    "WHERE key = :g AND day = :d AND used + reserved + :est <= :cap RETURNING count")
_COUNT = text("SELECT count FROM factcheck_quota WHERE key = :k AND day = :d")
_SETTLE = text(
    "UPDATE factcheck_quota SET reserved = GREATEST(reserved - :est, 0), used = used + :actual, updated_at = now() "
    "WHERE key IN (:k, :g) AND day = :d")


class FactcheckQuota:
    """factcheck_quota 표로 예약·정산한다. 세션 팩토리 하나를 받아 호출마다 짧은 트랜잭션을 연다."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], limits: QuotaLimits = QuotaLimits(),
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._factory = factory
        self.limits = limits
        self._now = now

    def day(self) -> str:
        """오늘(KST) YYYYMMDD."""
        return _kst_day(self._now())

    async def reserve(self, key: str, est: int, *, count_run: bool = True) -> Reservation:
        """키·전체 행에 est 토큰을 예약한다(count_run이면 실행 횟수도 1 늘린다). 상한을 넘으면 QuotaExceeded(롤백)."""
        d = self.day()
        inc = 1 if count_run else 0
        async with self._factory() as db:
            async with db.begin():
                await db.execute(_PURGE, {"g": GLOBAL_KEY, "d": d})
                await db.execute(_ENSURE, {"k": key, "g": GLOBAL_KEY, "d": d})
                took = (await db.execute(_TAKE_KEY, {"k": key, "d": d, "inc": inc, "est": est,
                                                     "runs": self.limits.runs, "cap": self.limits.key_tokens})).first()
                if took is None:
                    count = (await db.execute(_COUNT, {"k": key, "d": d})).scalar() or 0
                    raise QuotaExceeded("cap_runs" if count_run and count >= self.limits.runs else "cap_key_tokens")
                took = (await db.execute(_TAKE_GLOBAL, {"g": GLOBAL_KEY, "d": d, "inc": inc, "est": est,
                                                        "cap": self.limits.global_tokens})).first()
                if took is None:
                    raise QuotaExceeded("cap_global")  # 예외로 begin()이 롤백한다(키 행 예약도 함께 취소)
        return Reservation(key, d, est)

    async def settle(self, res: Reservation, actual_tokens: int) -> None:
        """예약을 풀고 실제 입력 토큰을 더한다(예약한 날의 행에)."""
        async with self._factory() as db:
            async with db.begin():
                await db.execute(_SETTLE, {"k": res.key, "g": GLOBAL_KEY, "d": res.day, "est": res.est,
                                           "actual": max(0, int(actual_tokens))})


class AnonKeyer:
    """익명 키 = sha256(일별 솔트 + IP). 솔트는 프로세스 메모리에만 두고 날이 바뀌면 버린다(원 IP를 저장하지 않고,
    지난 날 키와 연결할 수 없다). 재시작하면 그날 솔트가 바뀌어 익명 실행 횟수가 다시 시작된다(전체 상한은 DB라 그대로).
    # ponytail: 단일 인스턴스 메모리 솔트, 다중 인스턴스면 공유 저장소로
    """

    def __init__(self, day: Callable[[], str] = lambda: _kst_day(datetime.now(timezone.utc))):
        self._day = day
        self._salt_day: str | None = None
        self._salt = b""

    def key(self, ip: str) -> str:
        """오늘 솔트로 IP를 해시한 64자 16진수."""
        d = self._day()
        if d != self._salt_day:
            self._salt_day, self._salt = d, secrets.token_bytes(32)
        return hashlib.sha256(self._salt + ip.encode("utf-8")).hexdigest()
