# tests/evidence/conftest.py
"""근거 판정 테스트 공용 가짜 객체: 메모리 Redis, 장애 Redis, JEV 응답 본문."""
from __future__ import annotations

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
