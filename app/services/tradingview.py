"""TradingView 연동 서비스.

1) Webhook 신호 수신: TradingView 알림 메시지(JSON)를 받아 API 키로 사용자를 식별하고
   모의투자 계좌에 주문(source=TRADINGVIEW)을 넣은 뒤 Slack/Telegram 등으로 전달한다.
2) Strategy Tester ↔ LEAN 교차 검증: 트레이딩뷰 Strategy Tester 성과(직접 입력 또는
   '거래 목록' CSV 내보내기)를 LEAN 백테스트 결과와 나란히 비교하고 차이 원인을 짚어준다.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey, WebhookSignal, StrategyComparison
from app.services import paper_trading as pt
from app.services import risk_guard

logger = logging.getLogger(__name__)

ORDER_SOURCE = "TRADINGVIEW"
DUPLICATE_WINDOW_MIN = 1  # 같은 종목·방향 알림이 1분 안에 반복 전송되면 중복으로 본다 (TradingView 재전송 방지)

# TradingView 알림 메시지에 그대로 붙여 쓰는 템플릿 (플레이스홀더는 TradingView가 치환)
ALERT_TEMPLATE = {
    "token": "<발급한 API 키>",
    "strategy": "{{strategy.order.alert_message}}",
    "ticker": "{{ticker}}",
    "action": "{{strategy.order.action}}",
    "contracts": "{{strategy.order.contracts}}",
    "price": "{{close}}",
    "time": "{{timenow}}",
    "comment": "{{strategy.order.comment}}",
}


class WebhookError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ── Webhook ─────────────────────────────────────────────────────────────


async def resolve_user_by_token(db: AsyncSession, raw_token: str) -> ApiKey:
    raw = (raw_token or "").strip()
    if not raw:
        raise WebhookError(401, "token(API 키)이 필요합니다. 모의투자 › Open API 화면에서 발급하세요.")
    key_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    key = (await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True)))).scalar_one_or_none()
    if not key:
        raise WebhookError(401, "유효하지 않거나 폐기된 API 키입니다.")
    return key


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return default


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def normalize_ticker(raw: str) -> str:
    """TradingView 티커(KRX:005930, NASDAQ:AAPL, 005930.KS) → 모의투자 심볼."""
    t = (raw or "").strip().upper()
    if ":" in t:
        exch, sym = t.split(":", 1)
        if exch in ("KRX", "KOSPI", "KOSDAQ") and re.fullmatch(r"\d{6}", sym):
            return sym  # resolve_stock이 KOSPI/KOSDAQ 접미어 판별
        return sym
    return t


def parse_alert(payload: dict) -> dict:
    """알림 JSON을 {symbol, side, quantity, price, strategy, comment}로 정리."""
    action = str(payload.get("action") or payload.get("side") or payload.get("order") or "").strip().lower()
    side = "BUY" if action in ("buy", "long", "매수") else "SELL" if action in ("sell", "short", "매도") else "ALERT"
    return {
        "symbol": normalize_ticker(str(payload.get("ticker") or payload.get("symbol") or "")),
        "side": side,
        "quantity": _to_int(payload.get("contracts") or payload.get("quantity") or payload.get("qty"), 0),
        "price": _to_float(payload.get("price") or payload.get("close"), 0.0),
        "strategy": str(payload.get("strategy") or payload.get("strategy_name") or "")[:100],
        "comment": str(payload.get("comment") or payload.get("message") or "")[:200],
        "dry_run": bool(payload.get("dry_run") in (True, "true", "1", 1)),
    }


async def handle_alert(db: AsyncSession, payload: dict) -> dict:
    """Webhook 본문 처리: 사용자 식별 → 중복 검사 → 모의 주문 → 이력 저장. 커밋은 호출자."""
    key = await resolve_user_by_token(db, str(payload.get("token") or payload.get("api_key") or ""))
    user_id: uuid.UUID = key.user_id
    alert = parse_alert(payload)
    safe_payload = {k: v for k, v in payload.items() if k not in ("token", "api_key")}

    sig = WebhookSignal(
        user_id=user_id, provider="tradingview", strategy=alert["strategy"], symbol=alert["symbol"],
        side=alert["side"], quantity=alert["quantity"], signal_price=alert["price"], payload=safe_payload,
    )
    db.add(sig)

    if not alert["symbol"]:
        sig.status, sig.message = "rejected", "ticker/symbol 이 없습니다."
    elif alert["side"] == "ALERT" or alert["dry_run"] or alert["quantity"] <= 0:
        sig.status = "alert"
        sig.message = "주문 없이 알림만 전달했습니다." + (" (dry_run)" if alert["dry_run"] else "" if alert["side"] != "ALERT" else " (action 미지정)")
    elif not await risk_guard.acquire_order_slot(f"tv:{user_id}", alert["symbol"], alert["side"].lower(), DUPLICATE_WINDOW_MIN):
        sig.status, sig.message = "duplicate", f"{DUPLICATE_WINDOW_MIN}분 내 동일 종목·방향 알림 → 중복으로 무시"
    else:
        try:
            res = await pt.stock_order(db, user_id, alert["symbol"], alert["side"], alert["quantity"], source=ORDER_SOURCE)
            sig.symbol, sig.fill_price, sig.status = res["symbol"], float(res["price"]), "filled"
            sig.message = f"{res['name']} {alert['side']} {alert['quantity']}주 @ {res['price']:,.0f} 모의 체결"
        except pt.PaperTradeError as exc:
            sig.status, sig.message = "error", str(exc)[:300]
            await risk_guard.release_order_slot(f"tv:{user_id}", alert["symbol"], alert["side"].lower())

    key.last_used_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    key.call_count = (key.call_count or 0) + 1
    await db.flush()
    await db.refresh(sig)
    return {"user_id": str(user_id), "signal": signal_to_dict(sig), "alert": alert}


def signal_to_dict(s: WebhookSignal) -> dict:
    return {"id": str(s.id), "provider": s.provider, "strategy": s.strategy, "symbol": s.symbol, "side": s.side,
            "quantity": s.quantity, "signal_price": s.signal_price, "fill_price": s.fill_price, "status": s.status,
            "message": s.message, "payload": s.payload, "created_at": s.created_at.isoformat() if s.created_at else None}


async def list_signals(db: AsyncSession, user_id: uuid.UUID, limit: int = 50) -> list[dict]:
    rows = (await db.execute(select(WebhookSignal).where(WebhookSignal.user_id == user_id)
                             .order_by(WebhookSignal.created_at.desc()).limit(limit))).scalars().all()
    return [signal_to_dict(r) for r in rows]


# ── Strategy Tester CSV 파싱 ─────────────────────────────────────────────

_COL_ALIASES = {
    "type": ("type", "타입", "유형"),
    "profit_pct": ("profit %", "profit%", "p&l %", "net p&l %", "수익 %", "손익 %"),
    "cum_profit_pct": ("cum. profit %", "cumulative profit %", "cum profit %", "누적 수익 %"),
    "drawdown_pct": ("drawdown %", "draw down %", "낙폭 %"),
}


def _find_col(header: list[str], key: str) -> int | None:
    lowered = [h.strip().lower() for h in header]
    for alias in _COL_ALIASES[key]:
        for i, h in enumerate(lowered):
            if h == alias or h.startswith(alias):
                return i
    return None


def parse_trade_list_csv(text: str) -> dict:
    """TradingView Strategy Tester '거래 목록(List of Trades)' CSV → 성과지표.

    Exit(청산) 행의 'Profit %'로 거래별 손익을 모으고, 누적 곡선에서 MDD를 계산한다.
    """
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise ValueError("CSV 내용이 비어 있습니다.")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        raise ValueError("CSV 헤더를 읽을 수 없습니다.")
    ci_type, ci_pp = _find_col(header, "type"), _find_col(header, "profit_pct")
    if ci_pp is None:
        raise ValueError("'Profit %' 열을 찾을 수 없습니다. Strategy Tester › 거래 목록 › 내보내기(CSV)를 사용하세요.")
    profits: list[float] = []
    for row in reader:
        if len(row) <= ci_pp:
            continue
        typ = row[ci_type].strip().lower() if ci_type is not None and len(row) > ci_type else "exit"
        if not (typ.startswith("exit") or typ.startswith("close") or typ.startswith("청산")):
            continue
        cell = row[ci_pp].strip().replace("%", "").replace(",", "")
        if cell in ("", "-", "—"):
            continue
        try:
            profits.append(float(cell))
        except ValueError:
            continue
    if not profits:
        raise ValueError("청산(Exit) 거래의 Profit % 값을 찾지 못했습니다.")
    equity, peak, mdd = 1.0, 1.0, 0.0
    for p in profits:
        equity *= 1 + p / 100
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
    wins = sum(1 for p in profits if p > 0)
    return {
        "net_profit_pct": round((equity - 1) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "total_trades": len(profits),
        "win_rate_pct": round(wins / len(profits) * 100, 2),
        "avg_trade_pct": round(sum(profits) / len(profits), 3),
        "source": "TradingView 거래 목록 CSV",
    }


# ── TV ↔ LEAN 비교 ─────────────────────────────────────────────────────


def lean_metrics_from_result(res: dict) -> dict:
    stats = res.get("lean_statistics") or {}

    def _pct(v):
        try:
            return round(float(str(v).replace("%", "").replace(",", "")), 2)
        except (TypeError, ValueError):
            return None

    return {
        "net_profit_pct": res.get("strategy_return_pct"),
        "max_drawdown_pct": res.get("max_drawdown_pct"),
        "total_trades": res.get("trade_count") if res.get("trade_count") is not None else _pct(stats.get("Total Orders")),
        "win_rate_pct": _pct(stats.get("Win Rate")),
        "sharpe_ratio": res.get("sharpe_ratio"),
        "engine": res.get("engine"),
        "lean_ok": res.get("lean_ok"),
        "lean_statistics": stats,
        "source": "QuantConnect LEAN",
    }


def compare_metrics(tv: dict, lean: dict, ticker: str = "", strategy: str = "") -> dict:
    """항목별 차이와 판정, 차이가 나는 전형적 원인을 정리한다."""
    keys = [("net_profit_pct", "누적 수익률(%)"), ("max_drawdown_pct", "최대 낙폭 MDD(%)"),
            ("total_trades", "거래 횟수"), ("win_rate_pct", "승률(%)"), ("sharpe_ratio", "샤프 비율")]
    rows, diff = [], {}
    for k, label in keys:
        a, b = tv.get(k), lean.get(k)
        d = None if a is None or b is None else round(float(b) - float(a), 2)
        if k == "max_drawdown_pct" and a is not None and b is not None:
            d = round(abs(float(b)) - abs(float(a)), 2)  # 낙폭은 절대값 기준
        rows.append({"key": k, "label": label, "tradingview": a, "lean": b, "diff": d})
        diff[k] = d

    ret_d, mdd_d = diff.get("net_profit_pct"), diff.get("max_drawdown_pct")
    if ret_d is None and mdd_d is None:
        verdict = "비교 불가"
    else:
        worst = max(abs(ret_d or 0), abs(mdd_d or 0))
        verdict = "일치" if worst <= 3 else "부분 일치" if worst <= 10 else "불일치"

    causes = []
    if ret_d is not None and abs(ret_d) > 3:
        causes.append("수수료·슬리피지 설정 차이: TradingView Properties(Commission/Slippage)와 LEAN 모델(기본 0)이 다르면 수익률이 체계적으로 벌어집니다.")
        causes.append("체결 시점 차이: TradingView는 봉 마감 신호를 다음 봉 시가에 체결(기본)하고, 이 LEAN 예시는 종가 기준 다음 거래일 반영(look-ahead 방지)입니다.")
    tt, lt = tv.get("total_trades"), lean.get("total_trades")
    if tt is not None and lt is not None and tt != lt:
        if float(tt) > float(lt):
            causes.append("거래 횟수: TradingView가 더 많음 → calc_on_every_tick / 봉 내 재진입, 피라미딩(pyramiding) 설정을 확인하세요.")
        else:
            causes.append("거래 횟수: LEAN이 더 많음 → 이동평균 기간·돌파 윈도우 파라미터와 데이터 시작일(워밍업 구간) 차이를 확인하세요.")
    if mdd_d is not None and abs(mdd_d) > 3:
        causes.append("MDD 산정 방식: TradingView는 거래 단위 낙폭(청산 손익 누적) 기준, LEAN은 일별 평가액 기준이라 보유 중 미실현 손실이 LEAN에서 더 크게 잡힙니다.")
    causes.append("데이터 소스: TradingView(거래소 원장 시세) vs Yahoo Finance(수정주가·결측 가능) — 배당·액면분할 조정 여부가 다를 수 있습니다.")
    if not lean.get("lean_ok"):
        causes.append("LEAN 엔진이 실행되지 않아 pandas 계산치로 대체되었습니다. LEAN_MODE 설정 후 다시 실행하면 LEAN 원본 통계로 비교됩니다.")

    return {"ticker": ticker, "strategy": strategy, "verdict": verdict, "rows": rows, "diff": diff, "causes": causes,
            "tradingview": tv, "lean": {k: v for k, v in lean.items() if k != "lean_statistics"}}


def comparison_to_dict(c: StrategyComparison) -> dict:
    return {"id": str(c.id), "ticker": c.ticker, "strategy": c.strategy, "start_date": c.start_date, "end_date": c.end_date,
            "verdict": c.verdict, "tv_metrics": c.tv_metrics, "lean_metrics": c.lean_metrics, "diff": c.diff,
            "causes": c.causes, "created_at": c.created_at.isoformat() if c.created_at else None}


async def list_comparisons(db: AsyncSession, user_id: uuid.UUID, limit: int = 30) -> list[dict]:
    rows = (await db.execute(select(StrategyComparison).where(StrategyComparison.user_id == user_id)
                             .order_by(StrategyComparison.created_at.desc()).limit(limit))).scalars().all()
    return [comparison_to_dict(r) for r in rows]
