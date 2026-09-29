"""TradingView 연동 모델: Webhook 신호 수신 이력 · Strategy Tester ↔ LEAN 교차 검증 이력."""
from __future__ import annotations

import uuid

from sqlalchemy import Float, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class WebhookSignal(Base, UUIDPkMixin, CreatedAtMixin):
    """TradingView 알림(Webhook)으로 들어온 투자 신호."""

    __tablename__ = "webhook_signals"
    __table_args__ = (Index("ix_webhook_signals_user_created", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="tradingview")
    strategy: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    side: Mapped[str] = mapped_column(String(10), nullable=False, default="")       # BUY | SELL | ALERT
    quantity: Mapped[int] = mapped_column(nullable=False, default=0)
    signal_price: Mapped[float] = mapped_column(Float, nullable=False, default=0)   # 알림에 담긴 가격({{close}})
    fill_price: Mapped[float] = mapped_column(Float, nullable=False, default=0)     # 모의 체결가
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="received")  # filled | alert | rejected | duplicate | error
    message: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class StrategyComparison(Base, UUIDPkMixin, CreatedAtMixin):
    """TradingView Strategy Tester 결과 vs QuantConnect LEAN 백테스트 비교."""

    __tablename__ = "strategy_comparisons"
    __table_args__ = (Index("ix_strategy_comparisons_user_created", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy: Mapped[str] = mapped_column(String(40), nullable=False)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    end_date: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    verdict: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    tv_metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    lean_metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    diff: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    causes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
