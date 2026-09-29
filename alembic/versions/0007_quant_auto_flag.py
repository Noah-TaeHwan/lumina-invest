"""broker_settings.quant_auto_enabled — 자동매매 활성 플래그 (Celery Beat 10분 주기 실행용)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("broker_settings", sa.Column("quant_auto_enabled", sa.Boolean, nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("broker_settings", "quant_auto_enabled")
