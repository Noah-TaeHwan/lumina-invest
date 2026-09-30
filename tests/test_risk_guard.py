"""자동매매 위험관리: 비중 한도 · 일손실 한도 · 중복 주문 방지(메모리 폴백)."""
import asyncio

from app.services import risk_guard as rg


def test_cap_buy_quantity_reduces_or_blocks():
    # 총자산 1,000만원, 한도 30% → 종목당 300만원. 이미 200만원 보유, 주가 10만원 → 10주까지만
    qty, note = rg.cap_buy_quantity(50, 100_000, 2_000_000, 10_000_000, 30)
    assert qty == 10 and "축소" in note
    qty, note = rg.cap_buy_quantity(5, 100_000, 3_000_000, 10_000_000, 30)
    assert qty == 0 and "도달" in note
    assert rg.cap_buy_quantity(5, 100_000, 0, 10_000_000, 0) == (5, None)   # 0 = 비활성


def test_daily_loss_limit():
    assert rg.daily_pnl_pct(100, 97) == -3.0
    assert rg.daily_loss_breached(100, 97, 3.0)
    assert not rg.daily_loss_breached(100, 97.5, 3.0)
    assert not rg.daily_loss_breached(100, 50, 0)  # 한도 0 = 비활성


def test_order_slot_dedup_memory_fallback():
    """Redis 미연결 환경에서도 같은 종목·방향은 쿨다운 안에 1회만 허용."""
    rg._mem_keys.clear()
    ok1 = asyncio.run(rg.acquire_order_slot("u1", "005930.KS", "buy", 30))
    ok2 = asyncio.run(rg.acquire_order_slot("u1", "005930.KS", "buy", 30))
    other = asyncio.run(rg.acquire_order_slot("u1", "005930.KS", "sell", 30))
    assert ok1 and not ok2 and other
    asyncio.run(rg.release_order_slot("u1", "005930.KS", "buy"))
    assert asyncio.run(rg.acquire_order_slot("u1", "005930.KS", "buy", 30))


def test_orders_today_counter_memory_fallback():
    rg._mem_counters.clear()
    assert asyncio.run(rg.orders_today("u2")) == 0
    asyncio.run(rg.increment_orders_today("u2"))
    asyncio.run(rg.increment_orders_today("u2"))
    assert asyncio.run(rg.orders_today("u2")) == 2


def test_day_start_equity_is_fixed_after_first_call():
    rg._mem_values.clear()
    assert asyncio.run(rg.day_start_equity("u3", 1000.0)) == 1000.0
    assert asyncio.run(rg.day_start_equity("u3", 900.0)) == 1000.0
