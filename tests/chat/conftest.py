# tests/chat/conftest.py
"""기존 채팅 경로 테스트 공용.

- 저장 테스트는 근거 판정 테스트와 같은 빈 PostgreSQL 픽스처(pg)를 쓴다.
- RAG 테스트는 메모리 Qdrant와 가짜 임베딩을 쓴다(외부 호출 없음).
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest
from qdrant_client import AsyncQdrantClient

from app.services import rag_pipeline as rp
from tests.evidence.conftest import pg, pg_migrated  # noqa: F401

DIM = 768  # rag_pipeline이 만드는 컬렉션 차원


def vec(text: str) -> list[float]:
    """텍스트마다 다른 결정적 벡터."""
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    return np.random.default_rng(seed).normal(size=DIM).tolist()


class FakeEmbeddings:
    async def aembed_query(self, text):
        return vec(text)

    async def aembed_documents(self, texts):
        return [vec(t) for t in texts]


@pytest.fixture
def qdrant(monkeypatch):
    """rag_pipeline이 매번 만드는 클라이언트를 공유 메모리 클라이언트 하나로 바꾼다(close는 기록만)."""
    shared = AsyncQdrantClient(location=":memory:")
    closed = []

    async def close(*a, **k):
        closed.append(True)

    def factory(*a, **k):
        shared.close = close
        return shared

    monkeypatch.setattr(rp, "AsyncQdrantClient", factory)
    monkeypatch.setattr(rp, "_make_embeddings", lambda: FakeEmbeddings())
    shared.closed = closed
    return shared
