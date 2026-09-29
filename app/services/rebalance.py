"""리밸런싱 엔진 — 모의투자 계좌(현금 + 주식 포지션)를 목표 비중으로 되돌린다.

트리거
  TIME     : plan.time_period(monthly/quarterly/yearly) 주기의 next_run_at 도래
  DRIFT    : |현재 비중 − 목표 비중| 최대값 ≥ plan.drift_threshold_pct (%p)
  CASHFLOW : 입금·출금·배당 이벤트 금액 ≥ plan.cashflow_min_amount
  MANUAL   : 사용자가 화면에서 직접 실행

흐름
  snapshot()  → 현재 비중·이탈률 계산
  propose()   → 목표 비중과의 차액을 주문(매도 먼저, 매수 나중)으로 변환
  execute()   → paper_trading.stock_order 로 모의 체결하고 RebalanceRun 기록
  check_due() → TIME/DRIFT 트리거 점검(스케줄러·API 공용)
"""
from __future__ import annotations

import calendar
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CashflowEvent, RebalancePlan, RebalanceRun
from app.models.rebalance import CASHFLOW_KINDS, TIME_PERIODS
from app.services import paper_trading as pt

logger = logging.getLogger(__name__)

ORDER_SOURCE = "REBALANCE"


class RebalanceError(Exception):
    pass


# ── 플랜 ────────────────────────────────────────────────────────────────


async def get_plan(db: AsyncSession, user_id: uuid.UUID, create: bool = True) -> RebalancePlan | None:
    row = (await db.execute(select(RebalancePlan).where(RebalancePlan.user_id == user_id))).scalar_one_or_none()
    if row is None and create:
        row = RebalancePlan(user_id=user_id, targets=[])
        db.add(row)
        await db.flush()
    return row


def normalize_targets(raw: list[dict]) -> list[dict]:
    """[{symbol, name?, weight_pct}] 검증. 합계 100 초과 금지, 중복 심볼 병합."""
    merged: dict[str, dict] = {}
    for t in raw or []:
        symbol = pt.normalize_stock_symbol(str(t.get("symbol", "")))
        if not symbol:
            continue
        try:
            w = float(t.get("weight_pct", 0))
        except (TypeError, ValueError):
            raise RebalanceError(f"{symbol}: 비중은 숫자여야 합니다.")
        if w < 0 or w > 100:
            raise RebalanceError(f"{symbol}: 비중은 0~100 사이여야 합니다.")
        if symbol in merged:
            merged[symbol]["weight_pct"] += w
        else:
            merged[symbol] = {"symbol": symbol, "name": str(t.get("name") or symbol)[:100], "weight_pct": w}
    total = sum(t["weight_pct"] for t in merged.values())
    if total > 100.0001:
        raise RebalanceError(f"목표 비중 합계가 100%를 초과합니다 ({total:.1f}%). 잔여분은 현금으로 배분됩니다.")
    return [{**t, "weight_pct": round(t["weight_pct"], 2)} for t in merged.values()]


async def resolve_targets(raw: list[dict]) -> list[dict]:
    """입력 심볼을 모의투자 표준 심볼(005930 → 005930.KS)과 종목명으로 확정한다."""
    resolved = []
    for t in raw or []:
        sym = str(t.get("symbol", "")).strip()
        if not sym:
            continue
        try:
            info = await pt.resolve_stock(sym)
        except pt.PaperTradeError as exc:
            raise RebalanceError(str(exc))
        resolved.append({"symbol": info["symbol"], "name": info["name"], "weight_pct": t.get("weight_pct", 0)})
    return normalize_targets(resolved)


def _add_months(dt: datetime, months: int) -> datetime:
    month0 = dt.month - 1 + months
    year = dt.year + month0 // 12
    month = month0 % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def next_period_start(now: datetime, period: str) -> datetime | None:
    """다음 주기 시작 시각(월초/분기초/연초 00:00 UTC)."""
    if period not in TIME_PERIODS or period == "none":
        return None
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "monthly":
        return _add_months(first, 1)
    if period == "quarterly":
        q_start_month = ((now.month - 1) // 3) * 3 + 1
        return _add_months(first.replace(month=q_start_month), 3)
    return first.replace(month=1, year=now.year + 1)


def apply_plan_update(plan: RebalancePlan, data: dict) -> RebalancePlan:
    if "name" in data and data["name"]:
        plan.name = str(data["name"])[:60]
    if "is_active" in data:
        plan.is_active = bool(data["is_active"])
    if "targets" in data:
        plan.targets = normalize_targets(data["targets"])
    if "time_period" in data:
        period = str(data["time_period"] or "none")
        if period not in TIME_PERIODS:
            raise RebalanceError(f"지원하지 않는 주기입니다: {period}")
        if period != plan.time_period or plan.next_run_at is None:
            plan.next_run_at = next_period_start(datetime.now(timezone.utc), period)
        plan.time_period = period
    if "drift_enabled" in data:
        plan.drift_enabled = bool(data["drift_enabled"])
    if "drift_threshold_pct" in data:
        v = float(data["drift_threshold_pct"])
        if not 0.5 <= v <= 50:
            raise RebalanceError("허용 이탈률은 0.5~50%p 사이여야 합니다.")
        plan.drift_threshold_pct = v
    if "cashflow_enabled" in data:
        plan.cashflow_enabled = bool(data["cashflow_enabled"])
    if "cashflow_min_amount" in data:
        plan.cashflow_min_amount = max(0.0, float(data["cashflow_min_amount"]))
    if "auto_execute" in data:
        plan.auto_execute = bool(data["auto_execute"])
    if "min_order_amount" in data:
        plan.min_order_amount = max(0.0, float(data["min_order_amount"]))
    return plan


def plan_to_dict(plan: RebalancePlan) -> dict:
    stock_total = sum(float(t.get("weight_pct", 0)) for t in (plan.targets or []))
    return {
        "id": str(plan.id), "name": plan.name, "is_active": plan.is_active,
        "targets": plan.targets or [], "cash_weight_pct": round(100 - stock_total, 2),
        "time_period": plan.time_period,
        "next_run_at": plan.next_run_at.isoformat() if plan.next_run_at else None,
        "drift_enabled": plan.drift_enabled, "drift_threshold_pct": plan.drift_threshold_pct,
        "cashflow_enabled": plan.cashflow_enabled, "cashflow_min_amount": plan.cashflow_min_amount,
        "auto_execute": plan.auto_execute, "min_order_amount": plan.min_order_amount,
        "last_run_at": plan.last_run_at.isoformat() if plan.last_run_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
    }


# ── 스냅샷(현재 비중·이탈률) ──────────────────────────────────────────────


async def snapshot(db: AsyncSession, user_id: uuid.UUID, plan: RebalancePlan) -> dict:
    """현금 + 주식 포지션 기준 현재 비중과 목표 대비 이탈률."""
    account = await pt.get_account(db, user_id)
    positions = await pt.stock_positions(db, user_id)
    pos_map = {p["symbol"]: p for p in positions}
    stock_eval = sum(p["evalAmount"] for p in positions)
    total = float(account.cash) + stock_eval

    target_map = {t["symbol"]: float(t["weight_pct"]) for t in (plan.targets or [])}
    symbols = sorted(set(target_map) | set(pos_map))

    rows = []
    max_drift = 0.0
    for sym in symbols:
        p = pos_map.get(sym)
        cur_amt = p["evalAmount"] if p else 0.0
        cur_w = (cur_amt / total * 100) if total > 0 else 0.0
        tgt_w = target_map.get(sym, 0.0)
        drift = cur_w - tgt_w
        max_drift = max(max_drift, abs(drift))
        rows.append({
            "symbol": sym,
            "name": (p["name"] if p else next((t["name"] for t in plan.targets if t["symbol"] == sym), sym)),
            "quantity": p["quantity"] if p else 0,
            "price": p["currentPrice"] if p else None,
            "current_amount": round(cur_amt, 2),
            "current_weight_pct": round(cur_w, 2),
            "target_weight_pct": round(tgt_w, 2),
            "drift_pct": round(drift, 2),
            "in_plan": sym in target_map,
        })
    cash_w = (float(account.cash) / total * 100) if total > 0 else 100.0
    cash_target = 100 - sum(target_map.values())
    cash_drift = cash_w - cash_target
    max_drift = max(max_drift, abs(cash_drift))

    return {
        "total_asset": round(total, 2),
        "cash": round(float(account.cash), 2),
        "cash_weight_pct": round(cash_w, 2),
        "cash_target_pct": round(cash_target, 2),
        "cash_drift_pct": round(cash_drift, 2),
        "stock_eval": round(stock_eval, 2),
        "rows": rows,
        "max_drift_pct": round(max_drift, 2),
        "drift_exceeded": bool(plan.drift_enabled and max_drift >= plan.drift_threshold_pct and target_map),
    }


def _weights_from_snapshot(snap: dict) -> dict:
    w = {r["symbol"]: r["current_weight_pct"] for r in snap["rows"]}
    w["CASH"] = snap["cash_weight_pct"]
    return w


# ── 주문 산출 ────────────────────────────────────────────────────────────


async def propose(db: AsyncSession, user_id: uuid.UUID, plan: RebalancePlan, snap: dict | None = None) -> dict:
    """목표 비중과의 차액을 정수 주식 수량의 매도/매수 주문으로 변환한다.

    - 플랜에 없는 보유 종목은 전량 매도(목표 0%)
    - 매도를 먼저 산출해 확보되는 현금까지 포함해 매수 가능액 계산
    - 1주 미만 또는 min_order_amount 미만의 차액은 생략
    """
    if not plan.targets:
        raise RebalanceError("목표 비중이 비어 있습니다. 먼저 종목과 비중을 저장하세요.")
    snap = snap or await snapshot(db, user_id, plan)
    total = snap["total_asset"]
    if total <= 0:
        raise RebalanceError("평가 가능한 자산이 없습니다.")

    price_map: dict[str, float] = {}
    for r in snap["rows"]:
        if r["price"]:
            price_map[r["symbol"]] = float(r["price"])
    # 미보유 목표 종목의 현재가 조회
    for t in plan.targets:
        if t["symbol"] not in price_map:
            try:
                price_map[t["symbol"]] = float((await pt.resolve_stock(t["symbol"]))["price"])
            except Exception as exc:  # 시세 실패 종목은 건너뛰고 note에 남김
                logger.warning("리밸런싱 시세 조회 실패 %s: %s", t["symbol"], exc)

    sells, buys, skipped = [], [], []
    for r in snap["rows"]:
        sym = r["symbol"]
        price = price_map.get(sym)
        if not price:
            skipped.append({"symbol": sym, "reason": "시세 조회 실패"})
            continue
        target_amt = total * r["target_weight_pct"] / 100
        diff = target_amt - r["current_amount"]
        if abs(diff) < max(price, plan.min_order_amount):
            continue
        qty = int(abs(diff) // price)
        if qty <= 0:
            continue
        if diff < 0:
            qty = min(qty, int(r["quantity"]))
            if qty <= 0:
                continue
            sells.append({"symbol": sym, "name": r["name"], "side": "SELL", "quantity": qty,
                          "price": price, "amount": round(qty * price, 2), "status": "proposed"})
        else:
            buys.append({"symbol": sym, "name": r["name"], "side": "BUY", "quantity": qty,
                         "price": price, "amount": round(qty * price, 2), "status": "proposed"})

    # 매수는 (현금 + 매도대금) 범위 안에서만
    available = snap["cash"] + sum(o["amount"] for o in sells)
    for o in buys:
        if o["amount"] > available:
            qty = int(available // o["price"])
            if qty <= 0:
                o["quantity"], o["amount"], o["status"] = 0, 0.0, "skipped"
                o["error"] = "현금 부족"
                continue
            o["quantity"], o["amount"] = qty, round(qty * o["price"], 2)
        available -= o["amount"]
    buys = [o for o in buys if o["quantity"] > 0]

    orders = sells + buys
    est_cash = snap["cash"] + sum(o["amount"] for o in sells) - sum(o["amount"] for o in buys)
    return {
        "orders": orders,
        "skipped": skipped,
        "estimated_cash_after": round(est_cash, 2),
        "estimated_turnover": round(sum(o["amount"] for o in orders), 2),
        "snapshot": snap,
    }


# ── 실행 ────────────────────────────────────────────────────────────────


async def execute(db: AsyncSession, user_id: uuid.UUID, plan: RebalancePlan, trigger: str,
                  proposal: dict | None = None, note: str = "") -> RebalanceRun:
    """제안 주문을 모의 체결하고 RebalanceRun(executed)을 기록한다. 커밋은 호출자가 한다."""
    proposal = proposal or await propose(db, user_id, plan)
    before = _weights_from_snapshot(proposal["snapshot"])
    target = {t["symbol"]: float(t["weight_pct"]) for t in plan.targets}
    target["CASH"] = round(100 - sum(target.values()), 2)

    executed = []
    for o in proposal["orders"]:
        rec = dict(o)
        try:
            res = await pt.stock_order(db, user_id, o["symbol"], o["side"], int(o["quantity"]), source=ORDER_SOURCE)
            rec.update({"status": "filled", "price": res["price"], "amount": res["amount"]})
        except pt.PaperTradeError as exc:
            rec.update({"status": "failed", "error": str(exc)})
        executed.append(rec)

    after_snap = await snapshot(db, user_id, plan)
    run = RebalanceRun(
        user_id=user_id, plan_id=plan.id, trigger=trigger,
        status="executed" if any(o["status"] == "filled" for o in executed) else "skipped",
        total_asset=proposal["snapshot"]["total_asset"],
        max_drift_pct=proposal["snapshot"]["max_drift_pct"],
        before_weights=before, target_weights=target, after_weights=_weights_from_snapshot(after_snap),
        orders=executed, note=(note or "")[:300],
    )
    db.add(run)
    now = datetime.now(timezone.utc)
    plan.last_run_at = now
    if trigger == "TIME":
        plan.next_run_at = next_period_start(now, plan.time_period)
    await db.flush()
    return run


async def record_proposal(db: AsyncSession, user_id: uuid.UUID, plan: RebalancePlan, trigger: str,
                          proposal: dict, note: str = "") -> RebalanceRun:
    """자동 체결이 꺼진 플랜: 주문을 실행하지 않고 제안(proposed)만 남긴다."""
    target = {t["symbol"]: float(t["weight_pct"]) for t in plan.targets}
    target["CASH"] = round(100 - sum(target.values()), 2)
    run = RebalanceRun(
        user_id=user_id, plan_id=plan.id, trigger=trigger, status="proposed",
        total_asset=proposal["snapshot"]["total_asset"], max_drift_pct=proposal["snapshot"]["max_drift_pct"],
        before_weights=_weights_from_snapshot(proposal["snapshot"]), target_weights=target,
        orders=proposal["orders"], note=(note or "")[:300],
    )
    db.add(run)
    if trigger == "TIME":
        plan.next_run_at = next_period_start(datetime.now(timezone.utc), plan.time_period)
    await db.flush()
    return run


async def _has_recent_proposal(db: AsyncSession, user_id: uuid.UUID, trigger: str, hours: int = 24) -> bool:
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    row = (await db.execute(
        select(RebalanceRun.id).where(
            RebalanceRun.user_id == user_id, RebalanceRun.trigger == trigger,
            RebalanceRun.status == "proposed", RebalanceRun.created_at >= since,
        ).limit(1)
    )).scalar_one_or_none()
    return row is not None


async def trigger_rebalance(db: AsyncSession, user_id: uuid.UUID, plan: RebalancePlan, trigger: str,
                            note: str = "") -> RebalanceRun | None:
    """트리거 충족 시 auto_execute 여부에 따라 체결 또는 제안 기록."""
    try:
        proposal = await propose(db, user_id, plan)
    except RebalanceError as exc:
        logger.info("리밸런싱 제안 불가 user=%s: %s", user_id, exc)
        return None
    if not proposal["orders"]:
        return None
    if plan.auto_execute:
        return await execute(db, user_id, plan, trigger, proposal, note)
    if await _has_recent_proposal(db, user_id, trigger):
        return None  # 같은 트리거의 미처리 제안이 24시간 내 있으면 중복 생성 안 함
    return await record_proposal(db, user_id, plan, trigger, proposal, note)


# ── 트리거 점검 ─────────────────────────────────────────────────────────


async def check_due(db: AsyncSession, user_id: uuid.UUID, plan: RebalancePlan) -> dict:
    """TIME·DRIFT 트리거를 점검하고 충족 시 실행/제안. 결과 요약 반환."""
    now = datetime.now(timezone.utc)
    result: dict = {"time_due": False, "drift_due": False, "run_id": None, "trigger": None}
    if not plan.is_active or not plan.targets:
        return result

    if plan.time_period != "none" and plan.next_run_at and plan.next_run_at <= now:
        result["time_due"] = True
    snap = await snapshot(db, user_id, plan)
    result["max_drift_pct"] = snap["max_drift_pct"]
    if snap["drift_exceeded"]:
        result["drift_due"] = True

    trigger = "TIME" if result["time_due"] else ("DRIFT" if result["drift_due"] else None)
    if trigger:
        note = (f"{plan.time_period} 주기 도래" if trigger == "TIME"
                else f"최대 이탈 {snap['max_drift_pct']:.2f}%p ≥ 허용 {plan.drift_threshold_pct}%p")
        run = await trigger_rebalance(db, user_id, plan, trigger, note)
        if run:
            result["run_id"] = str(run.id)
            result["trigger"] = trigger
            result["status"] = run.status
        elif trigger == "TIME":
            plan.next_run_at = next_period_start(now, plan.time_period)  # 주문 없음 → 다음 주기로
    return result


async def check_all_due(session_factory) -> dict:
    """스케줄러용: 활성 플랜 전체 점검."""
    checked = executed = proposed = 0
    async with session_factory() as db:
        plans = (await db.execute(select(RebalancePlan).where(RebalancePlan.is_active.is_(True)))).scalars().all()
        for plan in plans:
            checked += 1
            try:
                r = await check_due(db, plan.user_id, plan)
                if r.get("status") == "executed":
                    executed += 1
                elif r.get("status") == "proposed":
                    proposed += 1
                await db.commit()
            except Exception:
                await db.rollback()
                logger.exception("리밸런싱 점검 실패 user=%s", plan.user_id)
    return {"checked": checked, "executed": executed, "proposed": proposed}


# ── 현금흐름(입금·출금·배당) ────────────────────────────────────────────


async def record_cashflow(db: AsyncSession, user_id: uuid.UUID, kind: str, amount: float,
                          symbol: str = "", memo: str = "") -> dict:
    """현금흐름을 모의계좌에 반영하고, 플랜 조건 충족 시 CASHFLOW 리밸런싱을 실행/제안한다."""
    kind = (kind or "").upper()
    if kind not in CASHFLOW_KINDS:
        raise RebalanceError("kind는 DEPOSIT, WITHDRAW, DIVIDEND 중 하나여야 합니다.")
    amount = float(amount)
    if amount <= 0:
        raise RebalanceError("금액은 0보다 커야 합니다.")

    account = await pt.get_account(db, user_id, lock=True)
    if kind == "WITHDRAW":
        if amount > account.cash:
            raise RebalanceError(f"출금 가능 현금이 부족합니다 (보유 {account.cash:,.0f}원).")
        account.cash = float(account.cash - amount)
    else:
        account.cash = float(account.cash + amount)
    if kind == "DIVIDEND" and symbol:
        symbol = pt.normalize_stock_symbol(symbol)

    event = CashflowEvent(user_id=user_id, kind=kind, amount=amount, symbol=symbol or "",
                          memo=(memo or "")[:200], cash_after=account.cash)
    db.add(event)
    await db.flush()
    await db.refresh(event)

    run = None
    plan = await get_plan(db, user_id, create=False)
    if plan and plan.is_active and plan.cashflow_enabled and plan.targets and amount >= plan.cashflow_min_amount:
        label = {"DEPOSIT": "입금", "WITHDRAW": "출금", "DIVIDEND": "배당"}[kind]
        run = await trigger_rebalance(db, user_id, plan, "CASHFLOW", f"{label} {amount:,.0f}원 발생")
        if run:
            event.rebalance_run_id = run.id
            await db.flush()
            await db.refresh(run)
    return {"event": cashflow_to_dict(event), "run": run_to_dict(run) if run else None}


def cashflow_to_dict(e: CashflowEvent) -> dict:
    return {"id": str(e.id), "kind": e.kind, "amount": e.amount, "symbol": e.symbol, "memo": e.memo,
            "cash_after": e.cash_after, "rebalance_run_id": str(e.rebalance_run_id) if e.rebalance_run_id else None,
            "created_at": e.created_at.isoformat() if e.created_at else None}


def run_to_dict(r: RebalanceRun) -> dict:
    return {"id": str(r.id), "trigger": r.trigger, "status": r.status, "total_asset": r.total_asset,
            "max_drift_pct": r.max_drift_pct, "before_weights": r.before_weights, "target_weights": r.target_weights,
            "after_weights": r.after_weights, "orders": r.orders, "note": r.note,
            "created_at": r.created_at.isoformat() if r.created_at else None}


async def list_runs(db: AsyncSession, user_id: uuid.UUID, limit: int = 30) -> list[dict]:
    rows = (await db.execute(select(RebalanceRun).where(RebalanceRun.user_id == user_id)
                             .order_by(RebalanceRun.created_at.desc()).limit(limit))).scalars().all()
    return [run_to_dict(r) for r in rows]


async def list_cashflows(db: AsyncSession, user_id: uuid.UUID, limit: int = 50) -> list[dict]:
    rows = (await db.execute(select(CashflowEvent).where(CashflowEvent.user_id == user_id)
                             .order_by(CashflowEvent.created_at.desc()).limit(limit))).scalars().all()
    return [cashflow_to_dict(e) for e in rows]


async def execute_proposal(db: AsyncSession, user_id: uuid.UUID, run_id: uuid.UUID) -> RebalanceRun:
    """제안(proposed) 상태의 실행 이력을 사용자가 승인해 체결한다(현재 시세로 재산출)."""
    row = (await db.execute(select(RebalanceRun).where(RebalanceRun.id == run_id, RebalanceRun.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise RebalanceError("제안을 찾을 수 없습니다.")
    if row.status != "proposed":
        raise RebalanceError("이미 처리된 제안입니다.")
    plan = await get_plan(db, user_id)
    new_run = await execute(db, user_id, plan, row.trigger, None, f"제안 승인 · {row.note}")
    row.status = "skipped"
    row.note = (row.note + " → 승인되어 새 실행으로 대체")[:300]
    return new_run
