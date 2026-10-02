"""근거 판정 기록: 판정 실행(evidence_runs)과 실행 안의 문장(evidence_claims)

chats 삭제가 SQL DELETE(스레드 삭제)라서 chat_id FK는 DB 수준 ON DELETE CASCADE로 둔다.
downgrade는 두 표만 지운다(chats는 바뀌지 않는다).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

U = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def upgrade() -> None:
    op.create_table(
        "evidence_runs",
        sa.Column("id", U(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("chat_id", U(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", U(), nullable=False),
        sa.Column("user_id", U(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("company", sa.String(200), nullable=False),
        sa.Column("corp_code", sa.String(16), nullable=False),
        sa.Column("rcept_no", sa.String(32), nullable=False, server_default=""),
        sa.Column("passages", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("jev_model", sa.String(64), nullable=False),
        sa.Column("generator_model", sa.String(200), nullable=False, server_default=""),
        sa.Column("calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cache_hits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_evidence_runs_conv_created", "evidence_runs", ["conversation_id", "created_at"])
    op.create_index("ix_evidence_runs_chat_created", "evidence_runs", ["chat_id", "created_at"])
    op.create_index("ix_evidence_runs_status_created", "evidence_runs", ["status", "created_at"])

    op.create_table(
        "evidence_claims",
        sa.Column("id", U(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", U(), sa.ForeignKey("evidence_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idx", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("start", sa.Integer, nullable=False),
        sa.Column("end", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("route", sa.String(16), nullable=True),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("source_idx", sa.Integer, nullable=True),
        sa.Column("s", postgresql.JSONB, nullable=True),
        sa.Column("c", postgresql.JSONB, nullable=True),
        sa.Column("lex", sa.Float, nullable=True),
        sa.Column("number_ok", postgresql.JSONB, nullable=True),
        sa.Column("jev_request_key", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Float, nullable=False, server_default="0"),
        sa.Column("cached", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("run_id", "idx", name="uq_evidence_claims_run_idx"),
    )


def downgrade() -> None:
    op.drop_table("evidence_claims")
    op.drop_index("ix_evidence_runs_status_created", table_name="evidence_runs")
    op.drop_index("ix_evidence_runs_chat_created", table_name="evidence_runs")
    op.drop_index("ix_evidence_runs_conv_created", table_name="evidence_runs")
    op.drop_table("evidence_runs")
