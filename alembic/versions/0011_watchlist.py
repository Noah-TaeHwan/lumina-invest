"""관심종목: 사용자별 종목 목록(watchlist_items)

(user_id, symbol) 유일, 목록 정렬용 (user_id, created_at) 색인만 둔다(사용자당 100행 이하, 모듈 D spec 결정 4-1·4-2).
downgrade는 이 표만 지운다(기존 표는 바뀌지 않는다).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

U = lambda: postgresql.UUID(as_uuid=True)  # noqa: E731


def upgrade() -> None:
    op.create_table(
        "watchlist_items",
        sa.Column("id", U(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", U(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("corp_code", sa.String(8), nullable=True),
        sa.Column("market", sa.String(16), nullable=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "symbol", name="uq_watchlist_user_symbol"),
    )
    op.create_index("ix_watchlist_items_user_created", "watchlist_items", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_watchlist_items_user_created", table_name="watchlist_items")
    op.drop_table("watchlist_items")
