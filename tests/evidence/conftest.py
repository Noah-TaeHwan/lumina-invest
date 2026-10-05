# tests/evidence/conftest.py
"""근거 판정 테스트 공용 가짜 객체: 메모리 Redis, 장애 Redis, JEV 응답 본문. 저장·API 테스트용 PostgreSQL 픽스처."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.lib import jev


class FakeRedis:
    """redis.asyncio에서 판정 코드가 쓰는 명령(get·set·incrby·expire)만 흉내 낸다."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl[key] = ex
        return True

    async def incrby(self, key, amount):
        self.data[key] = str(int(self.data.get(key, 0)) + int(amount))
        return int(self.data[key])

    async def expire(self, key, seconds):
        self.ttl[key] = seconds
        return True


class DownRedis:
    """모든 명령이 연결 오류를 낸다."""

    async def _fail(self, *a, **k):
        raise ConnectionError("redis down")

    get = set = incrby = expire = _fail


def payload(questions: dict, probs: dict | None = None, *, model: str = jev.MODEL, tokens: int = 100) -> dict:
    """질문마다 같은 확률을 담은 JEV 응답 본문."""
    probs = probs or {"supports": 0.8, "contradicts": 0.05, "says_nothing": 0.15}
    return {"model": model, "usage": {"input_tokens": tokens, "output_tokens": 5},
            "answers": {q: {"type": "choice", "probabilities": dict(probs)} for q in questions}}


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def down_redis():
    return DownRedis()


@pytest.fixture
def jev_payload():
    return payload


# ── 저장·API 테스트용 PostgreSQL(P2) ─────────────────────────────────────────
# 기존 모델이 JSONB·gen_random_uuid()를 쓰므로 SQLite로 대신하지 않는다. 빈 PostgreSQL URL을
# EVIDENCE_TEST_DATABASE_URL로 주면 0001→head 마이그레이션을 적용하고 테스트마다 표를 비운다.
# 없으면 DB 테스트만 이유를 밝히고 건너뛴다.
PG_ENV = "EVIDENCE_TEST_DATABASE_URL"
_TABLES = ("watchlist_items, judgment_updates, judgment_entries, evidence_claims, evidence_runs, chats, conversations, "
           "audit_events, users")


def _alembic(url: str):
    from alembic.config import Config

    from app.config import settings

    settings.DATABASE_URL = url  # alembic/env.py가 settings 값을 쓴다
    # alembic.ini를 읽지 않는다: env.py의 fileConfig가 이미 만든 로거(app.evidence.*)를 꺼서 로그 테스트를 깨뜨린다
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    return cfg


def alembic_upgrade(url: str, rev: str = "head") -> None:
    from alembic import command

    command.upgrade(_alembic(url), rev)


def alembic_downgrade(url: str, rev: str) -> None:
    from alembic import command

    command.downgrade(_alembic(url), rev)


@pytest.fixture(scope="session")
def pg_migrated():
    import os

    url = os.environ.get(PG_ENV)
    if not url:
        pytest.skip(f"{PG_ENV}가 없어 PostgreSQL 저장 테스트를 건너뛴다")
    alembic_upgrade(url)
    return url


@pytest.fixture
def pg(pg_migrated):
    """빈 표 상태의 DB URL."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def wipe():
        engine = create_async_engine(pg_migrated)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {_TABLES} CASCADE"))
        await engine.dispose()

    asyncio.run(wipe())
    return pg_migrated
