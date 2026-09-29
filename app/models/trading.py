from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPkMixin


PORTFOLIO_BOOK_PAPER = "PAPER"   # 모의투자·직접매매(WEB)·리밸런싱·TradingView 가 공유하는 모의계좌 장부
PORTFOLIO_BOOK_QUANT = "QUANT"   # 10분 자동매매 가상계좌 장부 (QuantVirtualAccount 현금과 짝)


class Portfolio(Base, UUIDPkMixin, UpdatedAtMixin):
    __tablename__ = "portfolio"
    __table_args__ = (UniqueConstraint("user_id", "symbol", "book", name="uq_portfolio_user_symbol_book"),)

    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    # 장부 구분 — 모의계좌(PAPER)와 자동매매 가상계좌(QUANT) 포지션을 섞지 않는다
    book: Mapped[str] = mapped_column(String(10), nullable=False, default=PORTFOLIO_BOOK_PAPER)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_price: Mapped[float] = mapped_column(Float, nullable=False, default=0)


class Order(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_user_created", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)  # buy | sell
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="filled")
    broker: Mapped[str] = mapped_column(String(20), nullable=False, default="virtual")
    # WEB(직접매매 화면) | PAPER(모의투자 화면) | OPENAPI(외부 Open API) | PINE(파인스크립트 실습) | QUANT(자동매매)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="WEB")


class BrokerSettings(Base, UUIDPkMixin, UpdatedAtMixin):
    """증권사 자격증명 + 퀀트 자동매매 전략설정 (Mongo 문서처럼 1유저 1행 통합)."""

    __tablename__ = "broker_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )
    broker: Mapped[str] = mapped_column(String(20), nullable=False, default="mock")
    app_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    app_secret: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    account_no: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    quant_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="paper")
    quant_symbol_source: Mapped[str] = mapped_column(String(10), nullable=False, default="ai")
    quant_selected_symbols: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    quant_ai_top_n: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    quant_per_trade_budget: Mapped[float] = mapped_column(Float, nullable=False, default=1_000_000)
    quant_buy_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    quant_sell_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)

    # ── 자동매매 위험관리 ──────────────────────────────────────────────────
    risk_daily_loss_limit_pct: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)   # 일중 손실 한도(%) 초과 시 비상 정지
    risk_max_position_pct: Mapped[float] = mapped_column(Float, nullable=False, default=30.0)      # 종목당 최대 비중(%)
    risk_max_orders_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=20)      # 하루 최대 자동 주문 수
    risk_cooldown_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30)            # 같은 종목·방향 재주문 금지 시간(분)
    risk_kill_switch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)         # 비상 정지 스위치
    risk_halt_reason: Mapped[str] = mapped_column(String(300), nullable=False, default="")         # 마지막 정지 사유
    # 자동매매 활성 플래그 — 프로세스 메모리 대신 DB에 두어 재시작·다중 인스턴스에서도 Celery Beat 이 이어서 실행
    quant_auto_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class QuantVirtualAccount(Base, UUIDPkMixin, CreatedAtMixin, UpdatedAtMixin):
    __tablename__ = "quant_virtual_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False, default=10_000_000)
    cash_balance: Mapped[float] = mapped_column(Float, nullable=False, default=10_000_000)


class CustomIndicator(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "custom_indicators"
    __table_args__ = (Index("ix_custom_indicators_user_created", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    base: Mapped[str] = mapped_column(String(20), nullable=False, default="rsi_ma")
    short_window: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    mid_window: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    rsi_period: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    buy_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=35.0)
