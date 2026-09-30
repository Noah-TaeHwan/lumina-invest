"""TradingView Webhook 신호 이력 + Strategy Tester↔LEAN 교차 검증 이력

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False, server_default="tradingview"),
        sa.Column("strategy", sa.String(100), nullable=False, server_default=""),
        sa.Column("symbol", sa.String(20), nullable=False, server_default=""),
        sa.Column("side", sa.String(10), nullable=False, server_default=""),
        sa.Column("quantity", sa.Integer, nullable=False, server_default="0"),
        sa.Column("signal_price", sa.Float, nullable=False, server_default="0"),
        sa.Column("fill_price", sa.Float, nullable=False, server_default="0"),
        sa.Column("status", sa.String(12), nullable=False, server_default="received"),
        sa.Column("message", sa.String(300), nullable=False, server_default=""),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_webhook_signals_user_created", "webhook_signals", ["user_id", "created_at"])

    op.create_table(
        "strategy_comparisons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("strategy", sa.String(40), nullable=False),
        sa.Column("start_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("end_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("verdict", sa.String(20), nullable=False, server_default=""),
        sa.Column("tv_metrics", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("lean_metrics", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("diff", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("causes", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_strategy_comparisons_user_created", "strategy_comparisons", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_strategy_comparisons_user_created", table_name="strategy_comparisons")
    op.drop_table("strategy_comparisons")
    op.drop_index("ix_webhook_signals_user_created", table_name="webhook_signals")
    op.drop_table("webhook_signals")
