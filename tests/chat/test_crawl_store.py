# tests/chat/test_crawl_store.py
"""크롤링 저장(_store_qdrant)은 같은 URL의 기존 점을 지우고 새 청크로 바꾼다.

point ID가 hash()에서 sha256으로 바뀌어 예전 ID의 점이 남고, 문서가 짧아지면 꼬리 청크도 남았다.
메모리 Qdrant와 가짜 임베딩으로 본다(외부 호출 없음)."""
from __future__ import annotations

import asyncio

import pytest
import qdrant_client
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams

from app.config import settings
from app.services import crawl
from tests.chat.conftest import DIM, vec

URL, OTHER = "https://example.com/docs/a.md", "https://example.com/docs/b.md"


class FakeOllama:
    async def embed(self, model, text):
        return vec(text)


@pytest.fixture
def shared(monkeypatch):
    client = AsyncQdrantClient(location=":memory:")

    async def close(*a, **k):
        pass

    def factory(*a, **k):
        client.close = close
        return client

    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", factory)
    return client


def test_restore_replaces_old_points_of_same_url(shared):
    coll = settings.QDRANT_COLLECTION

    async def go():
        await shared.create_collection(coll, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
        old = [PointStruct(id=1000 + i, vector=vec(f"old{i}"), payload={"url": URL, "text": f"old{i}"})
               for i in range(3)]  # 예전 hash() ID, 문서가 길던 때의 청크 3개
        other = PointStruct(id=2000, vector=vec("other"), payload={"url": OTHER, "text": "other"})
        upload = PointStruct(id=3000, vector=vec("up"), payload={
            "page_content": "up", "metadata": {"url": URL}, "owner_user_id": "u1"})  # 업로드 점은 건드리지 않는다
        await shared.upsert(coll, points=[*old, other, upload])
        n = await crawl._store_qdrant(["new0", "new1"], {"url": URL, "title": "a", "source": "web"}, FakeOllama())
        pts, _ = await shared.scroll(coll, with_payload=True, limit=100)
        return n, sorted((p.payload.get("text") or p.payload.get("page_content")) for p in pts)

    n, texts = asyncio.run(go())
    assert n == 2
    assert texts == ["new0", "new1", "other", "up"]


def test_failed_embedding_keeps_old_points(shared):
    """새 청크를 하나도 만들지 못하면 기존 점을 지우지 않는다."""
    coll = settings.QDRANT_COLLECTION

    class NoEmbed:
        async def embed(self, model, text):
            return []

    async def go():
        await shared.create_collection(coll, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
        await shared.upsert(coll, points=[PointStruct(id=1, vector=vec("old"), payload={"url": URL, "text": "old"})])
        n = await crawl._store_qdrant(["new"], {"url": URL}, NoEmbed())
        pts, _ = await shared.scroll(coll, with_payload=True)
        return n, [p.payload["text"] for p in pts]

    assert asyncio.run(go()) == (0, ["old"])
