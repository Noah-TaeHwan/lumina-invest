"""환율 변환 — 해외 종목 시세를 원화(KRW)로 환산한다.

Yahoo Finance 통화쌍 심볼(USD→KRW: "KRW=X", 그 외: "{CUR}KRW=X")을 10분 캐시로 조회한다.
조회 실패 시 마지막 성공값을 쓰고, 그것도 없으면 보수적 기본값(USD 1,350원)을 사용해 서비스가 멈추지 않게 한다.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_CACHE: dict[str, dict] = {}
_TTL = 600
_FALLBACK = {"USD": 1350.0, "JPY": 9.0, "EUR": 1480.0, "HKD": 173.0, "CNY": 187.0, "GBP": 1720.0}


def fx_symbol(currency: str) -> str:
    cur = (currency or "").upper()
    return "KRW=X" if cur == "USD" else f"{cur}KRW=X"


async def get_rate_to_krw(currency: str) -> float:
    """1 {currency} = ? KRW."""
    cur = (currency or "KRW").upper()
    if cur in ("KRW", ""):
        return 1.0
    now = time.time()
    hit = _CACHE.get(cur)
    if hit and now - hit["ts"] < _TTL:
        return hit["rate"]
    try:
        from app.services.stock import get_quote
        q = await get_quote(fx_symbol(cur))
        rate = float(q.get("price") or 0)
        if rate > 0:
            _CACHE[cur] = {"ts": now, "rate": rate}
            return rate
    except Exception as exc:
        logger.warning("환율 조회 실패 %s: %s", cur, exc)
    if hit:
        return hit["rate"]
    return _FALLBACK.get(cur, 1.0)


async def to_krw(price: float, currency: str) -> tuple[float, float]:
    """(원화 환산 가격, 적용 환율)."""
    rate = await get_rate_to_krw(currency)
    return float(price) * rate, rate
