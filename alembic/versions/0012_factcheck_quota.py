"""팩트체커 한도: 익명 키·전체 일일 카운터(factcheck_quota)

키 = sha256(IP + 일별 솔트)(원 IP 미저장) 또는 'global'(서버 전체), 일자 = KST YYYYMMDD.
count = 검수 실행 횟수, used = 정산된 JEV 입력 토큰, reserved = 호출 전에 잡아 둔 추정 토큰.
예약은 `UPDATE ... WHERE used + reserved + :est <= :cap RETURNING`로 원자적으로 한다(설계 Outside Voice #5).
원문·IP는 이 표에 들어가지 않는다. downgrade는 이 표만 지운다.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "factcheck_quota",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("day", sa.String(8), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("key", "day", name="pk_factcheck_quota"),
        sa.CheckConstraint("count >= 0 AND used >= 0 AND reserved >= 0", name="ck_factcheck_quota_nonneg"),
    )


def downgrade() -> None:
    op.drop_table("factcheck_quota")
