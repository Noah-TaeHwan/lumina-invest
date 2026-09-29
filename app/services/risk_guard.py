"""자동매매 위험관리(Risk Guard).

  - 중복 주문 방지 : 같은 종목·방향 주문을 쿨다운 시간 안에 다시 내지 않는다 (Redis SET NX EX, 미연결 시 메모리 폴백)
  - 일 주문 수 한도: 하루 자동 주문 건수 상한 (Redis INCR)
  - 종목 비중 한도 : 매수 후 종목 평가액이 총자산의 N%를 넘지 않도록 수량을 줄이거나 생략
  - 일손실 한도    : 당일 시작 자산 대비 손실률이 한도를 넘으면 비상 정지(kill switch) + 알림
  - 비상 정지      : kill switch가 켜져 있으면 사이클에서 주문을 전혀 내지 않는다 (수동/자동 모두)

모든 함수는 Redis 장애 시에도 자동매매를 막지 않도록 예외를 흡수하고 메모리 상태로 폴백한다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 메모리 폴백 상태 (Redis 미연결 시 사용). 프로세스 재시작 시 초기화된다.
_mem_keys: dict[str, float] = {}       # key -> expire_at(epoch)
_mem_counters: dict[str, int] = {}
_mem_values: dict[str, str] = {}


def _redis():
    try:
        from app.lib.redis_cache import get_redis
        return get_redis()
    except Exception:
        return None


def today_key(now: datetime | None = None) -> str:
    """거래일 키(KST 기준 날짜)."""
    return (now or datetime.now(timezone.utc)).astimezone(KST).strftime("%Y%m%d")


@dataclass
class RiskLimits:
    daily_loss_limit_pct: float = 3.0
    max_position_pct: float = 30.0
    max_orders_per_day: int = 20
    cooldown_min: int = 30
    kill_switch: bool = False

    @classmethod
    def from_row(cls, row) -> "RiskLimits":
        if row is None:
            return cls()
        return cls(
            daily_loss_limit_pct=float(getattr(row, "risk_daily_loss_limit_pct", 3.0) or 0),
            max_position_pct=float(getattr(row, "risk_max_position_pct", 30.0) or 0),
            max_orders_per_day=int(getattr(row, "risk_max_orders_per_day", 20) or 0),
            cooldown_min=int(getattr(row, "risk_cooldown_min", 30) or 0),
            kill_switch=bool(getattr(row, "risk_kill_switch", False)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ── 중복 주문 방지 (쿨다운) ─────────────────────────────────────────────


async def acquire_order_slot(user_id: str, symbol: str, side: str, cooldown_min: int) -> bool:
    """쿨다운 안에 같은 종목·방향 주문이 없었으면 슬롯을 잡고 True, 이미 있으면 False(중복)."""
    if cooldown_min <= 0:
        return True
    key = f"quant:order-slot:{user_id}:{symbol}:{side}"
    ttl = cooldown_min * 60
    r = _redis()
    if r is not None:
        try:
            ok = await r.set(key, str(time.time()), nx=True, ex=ttl)
            return bool(ok)
        except Exception as exc:
            logger.warning("risk_guard redis 실패, 메모리 폴백: %s", exc)
    now = time.time()
    exp = _mem_keys.get(key)
    if exp and exp > now:
        return False
    _mem_keys[key] = now + ttl
    return True


async def release_order_slot(user_id: str, symbol: str, side: str) -> None:
    """체결 실패 시 슬롯 반납(다음 사이클에 재시도 가능)."""
    key = f"quant:order-slot:{user_id}:{symbol}:{side}"
    r = _redis()
    if r is not None:
        try:
            await r.delete(key)
            return
        except Exception:
            pass
    _mem_keys.pop(key, None)


# ── 일 주문 수 ──────────────────────────────────────────────────────────


async def orders_today(user_id: str) -> int:
    key = f"quant:orders:{user_id}:{today_key()}"
    r = _redis()
    if r is not None:
        try:
            v = await r.get(key)
            return int(v or 0)
        except Exception:
            pass
    return _mem_counters.get(key, 0)


async def increment_orders_today(user_id: str) -> int:
    key = f"quant:orders:{user_id}:{today_key()}"
    r = _redis()
    if r is not None:
        try:
            n = await r.incr(key)
            await r.expire(key, 2 * 86400)
            return int(n)
        except Exception:
            pass
    _mem_counters[key] = _mem_counters.get(key, 0) + 1
    return _mem_counters[key]


# ── 일손실 한도 ─────────────────────────────────────────────────────────


async def day_start_equity(user_id: str, current_equity: float) -> float:
    """당일 첫 호출 시 현재 자산을 '시작 자산'으로 고정하고 이후 그 값을 돌려준다."""
    key = f"quant:day-equity:{user_id}:{today_key()}"
    r = _redis()
    if r is not None:
        try:
            await r.set(key, f"{current_equity:.2f}", nx=True, ex=2 * 86400)
            v = await r.get(key)
            return float(v) if v else current_equity
        except Exception:
            pass
    if key not in _mem_values:
        _mem_values[key] = f"{current_equity:.2f}"
    return float(_mem_values[key])


def daily_pnl_pct(start_equity: float, current_equity: float) -> float:
    if start_equity <= 0:
        return 0.0
    return round((current_equity / start_equity - 1) * 100, 3)


def daily_loss_breached(start_equity: float, current_equity: float, limit_pct: float) -> bool:
    if limit_pct <= 0:
        return False
    return daily_pnl_pct(start_equity, current_equity) <= -abs(limit_pct)


# ── 종목 비중 한도 ──────────────────────────────────────────────────────


def cap_buy_quantity(qty: int, price: float, existing_value: float, total_equity: float, max_position_pct: float) -> tuple[int, str | None]:
    """매수 후 종목 평가액이 total_equity × max_position_pct 를 넘지 않도록 수량을 줄인다.

    Returns (허용 수량, 조정 사유 또는 None). 허용 수량 0이면 매수 생략.
    """
    if max_position_pct <= 0 or total_equity <= 0 or price <= 0:
        return qty, None
    cap_value = total_equity * max_position_pct / 100
    room = cap_value - existing_value
    if room <= 0:
        return 0, f"종목 비중 한도 {max_position_pct:.0f}% 도달 (현재 {existing_value / total_equity * 100:.1f}%)"
    allowed = int(room // price)
    if allowed >= qty:
        return qty, None
    if allowed <= 0:
        return 0, f"종목 비중 한도 {max_position_pct:.0f}%로 1주도 추가 매수 불가"
    return allowed, f"종목 비중 한도 {max_position_pct:.0f}%로 수량 {qty}→{allowed}주 축소"


# ── 상태 조회 ───────────────────────────────────────────────────────────


async def risk_status(user_id: str, limits: RiskLimits, current_equity: float | None, halt_reason: str = "") -> dict:
    start = await day_start_equity(user_id, current_equity) if current_equity is not None else None
    pnl = daily_pnl_pct(start, current_equity) if (start and current_equity is not None) else None
    return {
        "limits": limits.to_dict(),
        "kill_switch": limits.kill_switch,
        "halt_reason": halt_reason,
        "date": today_key(),
        "day_start_equity": round(start, 2) if start else None,
        "current_equity": round(current_equity, 2) if current_equity is not None else None,
        "day_pnl_pct": pnl,
        "daily_loss_breached": bool(start and current_equity is not None and daily_loss_breached(start, current_equity, limits.daily_loss_limit_pct)),
        "orders_today": await orders_today(user_id),
        "orders_remaining": max(0, limits.max_orders_per_day - await orders_today(user_id)) if limits.max_orders_per_day > 0 else None,
    }
