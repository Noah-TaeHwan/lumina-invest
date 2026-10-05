"""관심종목(모듈 D spec 결정 4-1): 사용자별 종목 목록. 메모는 어디로도 나가지 않는다(결정 8-1)."""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class WatchlistItem(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_watchlist_user_symbol"),
        Index("ix_watchlist_items_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)  # Yahoo 심볼, 대문자로 정규화
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 표시 이름(검색 결과)
    # DART 고유번호. 국내 상장사이고 근거 모드 적재 회사와 정확히 하나 맞을 때만(결정 4-3)
    corp_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    market: Mapped[str | None] = mapped_column(String(16), nullable=True)  # KOSPI/KOSDAQ/거래소(서버 계산)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 짧은 메모
