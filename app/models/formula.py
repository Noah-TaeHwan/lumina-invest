"""자유 산식 커스텀 지표: 정의 · 버전 이력 · 계산 결과 저장."""
from __future__ import annotations

import uuid

from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPkMixin


class FormulaIndicator(Base, UUIDPkMixin, CreatedAtMixin, UpdatedAtMixin):
    __tablename__ = "formula_indicators"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_formula_indicators_user_name"),)

    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    indicator_expr: Mapped[str] = mapped_column(String(2000), nullable=False)
    buy_expr: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    sell_expr: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    checksum: Mapped[str] = mapped_column(String(16), nullable=False, default="")


class FormulaIndicatorVersion(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "formula_indicator_versions"
    __table_args__ = (UniqueConstraint("indicator_id", "version", name="uq_formula_versions_indicator_version"),)

    indicator_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("formula_indicators.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    indicator_expr: Mapped[str] = mapped_column(String(2000), nullable=False)
    buy_expr: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    sell_expr: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    checksum: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class FormulaIndicatorResult(Base, UUIDPkMixin, CreatedAtMixin):
    """(지표, 버전, 종목, 기간) 별 계산 결과 — 같은 날 같은 조합은 재계산 없이 재사용."""
    __tablename__ = "formula_indicator_results"
    __table_args__ = (Index("ix_formula_results_indicator_created", "indicator_id", "created_at"),)

    indicator_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("formula_indicators.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    period: Mapped[str] = mapped_column(String(10), nullable=False, default="2y")
    as_of: Mapped[str] = mapped_column(String(10), nullable=False, default="")   # 마지막 봉 날짜
    rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_signal: Mapped[str] = mapped_column(String(12), nullable=False, default="HOLD")
    costs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)      # commission/slippage/sl/tp
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)    # 백테스트 성과 요약
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)    # 시리즈 프리뷰 등 전체 결과
