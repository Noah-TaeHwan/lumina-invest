"""자동매매 위험관리 설정 (일손실 한도·종목 비중 한도·일 주문 수·쿨다운·비상정지)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("broker_settings", sa.Column("risk_daily_loss_limit_pct", sa.Float, nullable=False, server_default="3.0"))
    op.add_column("broker_settings", sa.Column("risk_max_position_pct", sa.Float, nullable=False, server_default="30.0"))
    op.add_column("broker_settings", sa.Column("risk_max_orders_per_day", sa.Integer, nullable=False, server_default="20"))
    op.add_column("broker_settings", sa.Column("risk_cooldown_min", sa.Integer, nullable=False, server_default="30"))
    op.add_column("broker_settings", sa.Column("risk_kill_switch", sa.Boolean, nullable=False, server_default=sa.text("false")))
    op.add_column("broker_settings", sa.Column("risk_halt_reason", sa.String(300), nullable=False, server_default=""))


def downgrade() -> None:
    for col in ("risk_halt_reason", "risk_kill_switch", "risk_cooldown_min", "risk_max_orders_per_day",
                "risk_max_position_pct", "risk_daily_loss_limit_pct"):
        op.drop_column("broker_settings", col)
