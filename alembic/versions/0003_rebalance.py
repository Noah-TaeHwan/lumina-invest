"""리밸런싱 엔진 (플랜·현금흐름 이벤트·실행 이력)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_pk():
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _user_fk(unique: bool = False):
    return sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, unique=unique)


def _created_at():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _updated_at():
    return sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "rebalance_plans",
        _uuid_pk(),
        _user_fk(unique=True),
        sa.Column("name", sa.String(60), nullable=False, server_default="내 리밸런싱 플랜"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("targets", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("time_period", sa.String(10), nullable=False, server_default="none"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drift_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("drift_threshold_pct", sa.Float, nullable=False, server_default="5.0"),
        sa.Column("cashflow_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("cashflow_min_amount", sa.Float, nullable=False, server_default="100000"),
        sa.Column("auto_execute", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("min_order_amount", sa.Float, nullable=False, server_default="10000"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
    )

    op.create_table(
        "cashflow_events",
        _uuid_pk(),
        _user_fk(),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False, server_default=""),
        sa.Column("memo", sa.String(200), nullable=False, server_default=""),
        sa.Column("cash_after", sa.Float, nullable=False, server_default="0"),
        sa.Column("rebalance_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        _created_at(),
    )
    op.create_index("ix_cashflow_events_user_created", "cashflow_events", ["user_id", "created_at"])

    op.create_table(
        "rebalance_runs",
        _uuid_pk(),
        _user_fk(),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trigger", sa.String(10), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="proposed"),
        sa.Column("total_asset", sa.Float, nullable=False, server_default="0"),
        sa.Column("max_drift_pct", sa.Float, nullable=False, server_default="0"),
        sa.Column("before_weights", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("target_weights", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("after_weights", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("orders", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        _created_at(),
    )
    op.create_index("ix_rebalance_runs_user_created", "rebalance_runs", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_rebalance_runs_user_created", table_name="rebalance_runs")
    op.drop_table("rebalance_runs")
    op.drop_index("ix_cashflow_events_user_created", table_name="cashflow_events")
    op.drop_table("cashflow_events")
    op.drop_table("rebalance_plans")
