"""portfolio.book — 모의계좌(PAPER)와 자동매매 가상계좌(QUANT) 포지션 분리

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29

기존 행은 모두 PAPER 장부로 간주한다. 자동매매(source=QUANT 주문)로 쌓인 포지션은 이후 사이클부터
QUANT 장부에 새로 기록되므로, 분리 전 자동매매 포지션은 모의계좌 포지션으로 남는다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("portfolio", sa.Column("book", sa.String(10), nullable=False, server_default="PAPER"))
    op.drop_constraint("uq_portfolio_user_symbol", "portfolio", type_="unique")
    op.create_unique_constraint("uq_portfolio_user_symbol_book", "portfolio", ["user_id", "symbol", "book"])


def downgrade() -> None:
    op.drop_constraint("uq_portfolio_user_symbol_book", "portfolio", type_="unique")
    op.create_unique_constraint("uq_portfolio_user_symbol", "portfolio", ["user_id", "symbol"])
    op.drop_column("portfolio", "book")
