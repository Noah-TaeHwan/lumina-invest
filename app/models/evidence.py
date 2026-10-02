"""근거 판정 기록(A-2 spec 결정 4-4): 메시지 하나에 대한 판정 실행 1건과 실행 안의 문장들."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UUIDPkMixin


class EvidenceRun(Base, UUIDPkMixin, CreatedAtMixin):
    __tablename__ = "evidence_runs"
    __table_args__ = (
        Index("ix_evidence_runs_conv_created", "conversation_id", "created_at"),
        Index("ix_evidence_runs_chat_created", "chat_id", "created_at"),
        Index("ix_evidence_runs_status_created", "status", "created_at"),
    )

    # 스레드 삭제가 chats를 SQL DELETE로 지우므로 DB 수준 cascade가 있어야 고아 행이 남지 않는다
    chat_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # pending / running / done / partial / failed / limited / skipped
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")  # auto / retry / rejudge
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    corp_code: Mapped[str] = mapped_column(String(16), nullable=False)
    rcept_no: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    passages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    jev_model: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_model: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    claims: Mapped[list["EvidenceClaim"]] = relationship(
        back_populates="run", order_by="EvidenceClaim.idx", cascade="all, delete-orphan", passive_deletes=True
    )


class EvidenceClaim(Base, UUIDPkMixin):
    __tablename__ = "evidence_claims"
    __table_args__ = (UniqueConstraint("run_id", "idx", name="uq_evidence_claims_run_idx"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("evidence_runs.id", ondelete="CASCADE"), nullable=False
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)
    # pending / supported / contradicted / no_evidence / not_claim / unjudged
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    route: Mapped[str | None] = mapped_column(String(16), nullable=True)  # lex_low / lex_high / jev / rule_not_claim
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)  # unjudged 사유(deadline, claim_cap 등)
    source_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    s: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    c: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    lex: Mapped[float | None] = mapped_column(Float, nullable=True)
    number_ok: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    jev_request_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    run: Mapped[EvidenceRun] = relationship(back_populates="claims")
