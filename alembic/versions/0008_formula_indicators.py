"""자유 산식 커스텀 지표: 정의·버전·계산 결과

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

U = lambda: postgresql.UUID(as_uuid=True)
J = lambda d: sa.Column  # noqa


def upgrade() -> None:
    op.create_table(
        "formula_indicators",
        sa.Column("id", U(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", U(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("description", sa.String(300), nullable=False, server_default=""),
        sa.Column("indicator_expr", sa.String(2000), nullable=False),
        sa.Column("buy_expr", sa.String(2000), nullable=False, server_default=""),
        sa.Column("sell_expr", sa.String(2000), nullable=False, server_default=""),
        sa.Column("params", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("current_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("checksum", sa.String(16), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "name", name="uq_formula_indicators_user_name"),
    )
    op.create_table(
        "formula_indicator_versions",
        sa.Column("id", U(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("indicator_id", U(), sa.ForeignKey("formula_indicators.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("indicator_expr", sa.String(2000), nullable=False),
        sa.Column("buy_expr", sa.String(2000), nullable=False, server_default=""),
        sa.Column("sell_expr", sa.String(2000), nullable=False, server_default=""),
        sa.Column("params", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("checksum", sa.String(16), nullable=False, server_default=""),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("indicator_id", "version", name="uq_formula_versions_indicator_version"),
    )
    op.create_table(
        "formula_indicator_results",
        sa.Column("id", U(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("indicator_id", U(), sa.ForeignKey("formula_indicators.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", U(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("checksum", sa.String(16), nullable=False, server_default=""),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("period", sa.String(10), nullable=False, server_default="2y"),
        sa.Column("as_of", sa.String(10), nullable=False, server_default=""),
        sa.Column("rows", sa.Integer, nullable=False, server_default="0"),
        sa.Column("latest_value", sa.Float, nullable=True),
        sa.Column("latest_signal", sa.String(12), nullable=False, server_default="HOLD"),
        sa.Column("costs", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("metrics", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_formula_results_indicator_created", "formula_indicator_results", ["indicator_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_formula_results_indicator_created", table_name="formula_indicator_results")
    op.drop_table("formula_indicator_results")
    op.drop_table("formula_indicator_versions")
    op.drop_table("formula_indicators")
