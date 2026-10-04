"""투자 판단 일지(모듈 C spec 결정 5-2): 판정 실행 하나에 대한 판단 기록(스냅샷 고정)과 다시 보기 원장(덧붙이기만)."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin

DECISIONS = ("consider_buy", "watch", "exclude")  # 매수 검토 / 관망 / 제외
UPDATE_KINDS = ("initial", "revisit")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class JudgmentEntry(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "judgment_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "run_id", name="uq_judgment_entries_user_run"),
        Index("ix_judgment_entries_user_created", "user_id", "created_at"),
        Index("ix_judgment_entries_user_corp_created", "user_id", "corp_code", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    # 원 판정 실행. FK를 걸지 않는다: 스레드 삭제로 실행이 사라져도 기록은 남는다(결정 5-4)
    run_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    corp_code: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_version: Mapped[str] = mapped_column(String(16), nullable=False)


class JudgmentUpdate(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "judgment_updates"
    __table_args__ = (
        CheckConstraint(_in("kind", UPDATE_KINDS), name="ck_judgment_updates_kind"),
        CheckConstraint(_in("decision", DECISIONS), name="ck_judgment_updates_decision"),
        CheckConstraint("conviction BETWEEN 1 AND 5", name="ck_judgment_updates_conviction"),
        Index("ix_judgment_updates_entry_created", "entry_id", "created_at"),
    )

    entry_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("judgment_entries.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    conviction: Mapped[int] = mapped_column(Integer, nullable=False)  # 내 확신 1~5
    memo: Mapped[str] = mapped_column(Text, nullable=False, default="")  # 2,000자 이하(API 검증)
    relied_claims: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # 기댄 문장 idx
    review_on: Mapped[date | None] = mapped_column(Date, nullable=True)  # 다시 볼 날짜(KST)
