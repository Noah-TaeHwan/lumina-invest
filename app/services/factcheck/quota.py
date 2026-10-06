# app/services/factcheck/quota.py
"""팩트체커 일일 한도: 익명 키별·서버 전체 JEV 토큰을 PostgreSQL에서 원자적으로 예약하고, 예약 ID로 한 번만 정산한다.

설계 D8·Outside Voice #5, 검수 결과(비용 상한 1·3·5·6, 남용 11):
- 표 factcheck_quota(key, day, count, used, reserved). key는 익명 키(sha256(일별 솔트 + IP)) 또는 'global'.
- 예약: 한 트랜잭션에서 `UPDATE ... SET reserved = reserved + :est WHERE used + reserved + :est <= :cap RETURNING`을
  키 행 → 전체 행 순서로 한다(항상 같은 순서라 교착이 없다). 어느 하나라도 행이 안 나오면 롤백하고 QuotaExceeded.
  같은 행을 노리는 동시 요청은 행 잠금으로 줄을 서고, 잠금이 풀린 뒤 WHERE를 다시 평가하므로 같은 잔여량을 둘이 통과할 수 없다.
  성공하면 같은 트랜잭션에서 예약 행(factcheck_reservations, ID)을 남긴다. 예약량은 상한 기준이다(metering.reservation_for).
- 예약에 실패하면 호출하지 않는다(호출부 책임: 예약이 성공해야 파이프라인을 시작한다).
- 정산(멱등): `UPDATE factcheck_reservations SET settled_at ... WHERE id = :id AND settled_at IS NULL RETURNING`이 행을
  돌려줄 때만 한도 행에서 예약분을 빼고 실제를 더한다. 한도 행이 두 개(키·전체) 다 바뀌지 않으면 되돌린다(SettleMismatch).
  실제가 음수·정수 아님이면 예약량 그대로 차감한다(0 정산 금지).
- 오래된 미정산 예약(서버가 죽었거나 정산 중 DB 오류)은 settle_stale이 예약량으로 정산한다(서버 시작 때·주기적으로).
- 익명 하루 실행 3회(count). 건너뛴 문장 수동 검수는 count_run=False로 토큰만 예약한다.
- 날짜는 KST. 지난 날의 익명 행은 예약할 때 지우되, 진행 중 예약이 있는 행(reserved > 0)은 남긴다(자정 넘김). 전체 행은 남긴다.
- 상한 값은 금액이 아니라 입력 토큰 수다. 공개 배포의 전체 상한 FACTCHECK_DAILY_GLOBAL_TOKENS는 노아가 공개 전에 정한다.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.factcheck import settings as fc_settings

KST = timezone(timedelta(hours=9))
GLOBAL_KEY = "global"
MAX_SENTENCES = 30  # 익명 1회 입력 전체 문장 상한(라우트의 422 기준과 같다)
STALE_S = 30 * 60


@dataclass(frozen=True)
class QuotaLimits:
    """하루 상한: 익명 키 실행 횟수·익명 키 토큰·서버 전체 토큰(기본값은 settings와 같다)."""

    runs: int = 3
    key_tokens: int = 2_000_000
    global_tokens: int = 3_000_000


def limits_from_env() -> QuotaLimits:
    """환경변수(FACTCHECK_DAILY_*)에서 상한을 읽는다."""
    s = fc_settings.load()
    return QuotaLimits(runs=s.FACTCHECK_DAILY_ANON_RUNS, key_tokens=s.FACTCHECK_DAILY_KEY_TOKENS,
                       global_tokens=s.FACTCHECK_DAILY_GLOBAL_TOKENS)


@dataclass(frozen=True)
class Reservation:
    """예약 하나. id로 한 번만 정산한다."""

    id: str
    key: str
    day: str
    est: int


class QuotaExceeded(Exception):
    """예약 실패. code ∈ {cap_runs, cap_key_tokens, cap_global}."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SettleMismatch(RuntimeError):
    """정산할 한도 행(키·전체)이 둘 다 있지 않다. 정산을 되돌렸다(예약은 미정산으로 남는다)."""


def _kst_day(now: datetime) -> str:
    return now.astimezone(KST).strftime("%Y%m%d")


def valid_tokens(v: Any) -> bool:
    """토큰 수로 받을 수 있는 값인가(bool이 아닌 0 이상 정수)."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


_ENSURE = text("INSERT INTO factcheck_quota (key, day) VALUES (:k, :d), (:g, :d) ON CONFLICT DO NOTHING")
_PURGE = text("DELETE FROM factcheck_quota WHERE key <> :g AND day < :d AND reserved = 0")
_TAKE_KEY = text(
    "UPDATE factcheck_quota SET count = count + :inc, reserved = reserved + :est, updated_at = now() "
    "WHERE key = :k AND day = :d AND (:inc = 0 OR count < :runs) AND used + reserved + :est <= :cap "
    "RETURNING count")
_TAKE_GLOBAL = text(
    "UPDATE factcheck_quota SET count = count + :inc, reserved = reserved + :est, updated_at = now() "
    "WHERE key = :g AND day = :d AND used + reserved + :est <= :cap RETURNING count")
_COUNT = text("SELECT count FROM factcheck_quota WHERE key = :k AND day = :d")
_RECORD = text("INSERT INTO factcheck_reservations (id, key, day, est, count_run) VALUES (:id, :k, :d, :est, :cr)")
_CLOSE = text("UPDATE factcheck_reservations SET settled_at = now(), actual = :actual "
              "WHERE id = :id AND settled_at IS NULL RETURNING key, day, est")
_SETTLE = text(
    "UPDATE factcheck_quota SET reserved = GREATEST(reserved - :est, 0), used = used + :actual, updated_at = now() "
    "WHERE key IN (:k, :g) AND day = :d")
_STALE = text("SELECT id, key, day, est FROM factcheck_reservations "
              "WHERE settled_at IS NULL AND created_at < now() - make_interval(secs => :s) ORDER BY created_at")


class FactcheckQuota:
    """factcheck_quota·factcheck_reservations 표로 예약·정산한다. 호출마다 짧은 트랜잭션을 연다."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], limits: QuotaLimits = QuotaLimits(),
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._factory = factory
        self.limits = limits
        self._now = now

    def day(self) -> str:
        """오늘(KST) YYYYMMDD."""
        return _kst_day(self._now())

    async def reserve(self, key: str, est: int, *, count_run: bool = True) -> Reservation:
        """키·전체 행에 est 토큰을 예약하고 예약 행을 남긴다(count_run이면 실행 횟수도 1). 넘으면 QuotaExceeded(롤백)."""
        d = self.day()
        inc = 1 if count_run else 0
        rid = secrets.token_hex(16)
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
                await db.execute(_RECORD, {"id": rid, "k": key, "d": d, "est": est, "cr": count_run})
        return Reservation(rid, key, d, est)

    async def settle(self, res: Reservation, actual_tokens: Any) -> bool:
        """예약 ID로 한 번만 정산한다. 이미 정산됐으면 False. 실제가 비정상이면 예약량으로."""
        actual = actual_tokens if valid_tokens(actual_tokens) else res.est
        async with self._factory() as db:
            async with db.begin():
                return await self._settle_in(db, res.id, actual)

    async def _settle_in(self, db: AsyncSession, rid: str, actual: int) -> bool:
        row = (await db.execute(_CLOSE, {"id": rid, "actual": actual})).first()
        if row is None:
            return False
        n = (await db.execute(_SETTLE, {"k": row.key, "g": GLOBAL_KEY, "d": row.day, "est": row.est,
                                        "actual": actual})).rowcount
        if n != 2:
            raise SettleMismatch(f"quota rows updated={n}")  # begin()이 되돌린다
        return True

    async def settle_stale(self, older_than_s: float = STALE_S) -> int:
        """older_than_s보다 오래된 미정산 예약을 예약량으로 정산하고 그 수를 돌려준다."""
        async with self._factory() as db:
            rows = (await db.execute(_STALE, {"s": float(older_than_s)})).all()
        n = 0
        for r in rows:
            async with self._factory() as db:
                async with db.begin():
                    n += await self._settle_in(db, r.id, r.est)
        return n


_SALT_GET = text("SELECT salt FROM factcheck_salt WHERE day = :d")
_SALT_PUT = text("INSERT INTO factcheck_salt (day, salt) VALUES (:d, :s) ON CONFLICT (day) DO NOTHING")
_SALT_PURGE = text("DELETE FROM factcheck_salt WHERE day < :d")


class AnonKeyer:
    """익명 키 = sha256(일별 솔트 + IP). 솔트는 DB(factcheck_salt)에 날짜별로 두어 재시작해도 같은 날은 같은 키다
    (재시작으로 익명 한도가 초기화되지 않는다). 날이 바뀌면 지난 날 솔트를 지워 지난 키와 IP를 다시 잇지 못한다.
    원 IP는 어디에도 저장하지 않는다. 그날 솔트는 메모리에도 둔다."""

    def __init__(self, factory: async_sessionmaker[AsyncSession],
                 day: Callable[[], str] = lambda: _kst_day(datetime.now(timezone.utc))):
        self._factory = factory
        self._day = day
        self._cache: tuple[str, bytes] | None = None

    async def _salt(self, d: str) -> bytes:
        if self._cache and self._cache[0] == d:
            return self._cache[1]
        async with self._factory() as db:
            async with db.begin():
                await db.execute(_SALT_PUT, {"d": d, "s": secrets.token_bytes(32)})  # 동시에 와도 하나만 남는다
                salt = (await db.execute(_SALT_GET, {"d": d})).scalar_one()
                await db.execute(_SALT_PURGE, {"d": d})
        self._cache = (d, bytes(salt))
        return self._cache[1]

    async def key(self, ip: str) -> str:
        """오늘 솔트로 IP를 해시한 64자 16진수."""
        return hashlib.sha256(await self._salt(self._day()) + ip.encode("utf-8")).hexdigest()
