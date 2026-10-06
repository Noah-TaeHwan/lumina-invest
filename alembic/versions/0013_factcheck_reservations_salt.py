"""팩트체커 예약 ID·일별 솔트: factcheck_reservations, factcheck_salt

- factcheck_reservations: 예약 하나 = 행 하나(ID). 정산은 `settled_at IS NULL`인 행만 한 번 바꾼다(멱등).
  서버 시작 때 오래된(30분) 미정산 행은 예약량(est)으로 정산한다. 원문·IP는 없다(key는 익명 키 해시).
- factcheck_salt: 익명 키 sha256(솔트 + IP)의 날짜별 솔트. 재시작해도 같은 날은 같은 키가 되게 DB에 두고,
  날이 바뀌면 지난 날 솔트를 지운다(지난 키와 IP를 다시 잇지 못한다).
0012(factcheck_quota)는 그대로 둔다. downgrade는 이 두 표만 지운다.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "factcheck_reservations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("day", sa.String(8), nullable=False),
        sa.Column("est", sa.BigInteger(), nullable=False),
        sa.Column("actual", sa.BigInteger(), nullable=True),
        sa.Column("count_run", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("est >= 0 AND (actual IS NULL OR actual >= 0)", name="ck_factcheck_reservations_nonneg"),
    )
    op.create_index("ix_factcheck_reservations_open", "factcheck_reservations", ["created_at"],
                    postgresql_where=sa.text("settled_at IS NULL"))
    op.create_table(
        "factcheck_salt",
        sa.Column("day", sa.String(8), primary_key=True),
        sa.Column("salt", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("factcheck_salt")
    op.drop_index("ix_factcheck_reservations_open", table_name="factcheck_reservations")
    op.drop_table("factcheck_reservations")
