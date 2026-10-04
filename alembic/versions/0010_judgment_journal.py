"""투자 판단 일지: 판단 기록(judgment_entries)과 다시 보기 원장(judgment_updates)

run_id에는 FK를 걸지 않는다(스레드 삭제로 판정 실행이 사라져도 기록은 남는다, 모듈 C spec 결정 5-4).
update는 기록 삭제에만 따라 지워진다(ON DELETE CASCADE). downgrade는 두 표만 지운다(기존 표는 바뀌지 않는다).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

U = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def upgrade() -> None:
    op.create_table(
        "judgment_entries",
        sa.Column("id", U(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", U(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("run_id", U(), nullable=False),
        sa.Column("company", sa.String(200), nullable=False),
        sa.Column("corp_code", sa.String(16), nullable=False),
        sa.Column("snapshot", postgresql.JSONB, nullable=False),
        sa.Column("snapshot_version", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "run_id", name="uq_judgment_entries_user_run"),
    )
    op.create_index("ix_judgment_entries_user_created", "judgment_entries", ["user_id", "created_at"])
    op.create_index("ix_judgment_entries_user_corp_created", "judgment_entries",
                    ["user_id", "corp_code", "created_at"])

    op.create_table(
        "judgment_updates",
        sa.Column("id", U(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("entry_id", U(), sa.ForeignKey("judgment_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("conviction", sa.Integer, nullable=False),
        sa.Column("memo", sa.Text, nullable=False, server_default=""),
        sa.Column("relied_claims", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("review_on", sa.Date, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('initial', 'revisit')", name="ck_judgment_updates_kind"),
        sa.CheckConstraint("decision IN ('consider_buy', 'watch', 'exclude')", name="ck_judgment_updates_decision"),
        sa.CheckConstraint("conviction BETWEEN 1 AND 5", name="ck_judgment_updates_conviction"),
    )
    op.create_index("ix_judgment_updates_entry_created", "judgment_updates", ["entry_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_judgment_updates_entry_created", table_name="judgment_updates")
    op.drop_table("judgment_updates")
    op.drop_index("ix_judgment_entries_user_corp_created", table_name="judgment_entries")
    op.drop_index("ix_judgment_entries_user_created", table_name="judgment_entries")
    op.drop_table("judgment_entries")
