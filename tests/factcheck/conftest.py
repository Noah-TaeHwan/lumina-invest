# tests/factcheck/conftest.py
"""팩트체커(T3) DB 픽스처: 빈 PostgreSQL에 마이그레이션 적용·팩트체커 표 비우기. 가짜 객체는 fc_support.py에 있다.

DB 픽스처는 tests/evidence/conftest.py와 같은 환경변수(EVIDENCE_TEST_DATABASE_URL)를 쓰고, 없으면 그 테스트만 건너뛴다.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.evidence.conftest import PG_ENV, alembic_upgrade

FC_TABLES = "factcheck_quota, factcheck_reservations, factcheck_salt"


@pytest.fixture(scope="session")
def fc_pg_migrated():
    """빈 PostgreSQL에 0001→head를 적용한 URL. 환경변수가 없으면 건너뛴다."""
    url = os.environ.get(PG_ENV)
    if not url:
        pytest.skip(f"{PG_ENV}가 없어 팩트체커 DB 테스트를 건너뛴다")
    alembic_upgrade(url)
    return url


@pytest.fixture
def fc_pg(fc_pg_migrated):
    """팩트체커 표(한도·예약·솔트)를 비운 DB URL."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def wipe():
        engine = create_async_engine(fc_pg_migrated)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {FC_TABLES}"))
        await engine.dispose()

    asyncio.run(wipe())
    return fc_pg_migrated
