# tests/chat/test_rag_pipeline.py
"""app/services/rag_pipeline.py: 메모리 Qdrant와 가짜 임베딩으로 검색·저장·삭제를 본다(외부 호출 없음).

A-1 spec 12절이 기록한 결함: 비동기 클라이언트 예외를 빈 결과로 삼키는 경로, 크롤링 payload 키(text)와
LangChain 키(page_content) 불일치.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from qdrant_client.http.models import Distance, PointStruct, VectorParams

from app.services import rag_pipeline as rp
from tests.chat.conftest import DIM, vec

COLL = "test_rag"


def run(coro):
    return asyncio.run(coro)


async def _seed(client, points):
    if not await client.collection_exists(COLL):
        await client.create_collection(COLL, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
    await client.upsert(COLL, points=points)


LC_POINT = PointStruct(id=1, vector=vec("메모리 반도체"), payload={
    "page_content": "메모리 반도체", "metadata": {"url": "upload://a.pdf", "title": "a.pdf", "source": "upload:u1:a.pdf"}})


# ── 비동기 클라이언트 예외를 빈 결과로 삼키는 경로 ─────────────────────────────

def test_search_returns_langchain_layout_hit(qdrant):
    async def go():
        await _seed(qdrant, [LC_POINT])
        return await rp.rag_search("메모리 반도체", top_k=3, collection=COLL)

    hits = run(go())
    assert [(h["text"], h["title"], h["url"], h["source"]) for h in hits] == [
        ("메모리 반도체", "a.pdf", "upload://a.pdf", "upload:u1:a.pdf")]
    assert hits[0]["score"] == pytest.approx(1.0, abs=1e-4)
    assert qdrant.closed


def test_store_chunks_then_search(qdrant):
    async def go():
        n = await rp.store_chunks(["첫 청크", "둘째 청크"], {"title": "b.pdf", "source": "upload:u1:b.pdf"},
                                  collection=COLL)
        return n, await rp.rag_search("둘째 청크", top_k=1, collection=COLL)

    n, hits = run(go())
    assert n == 2
    assert [(h["text"], h["source"]) for h in hits] == [("둘째 청크", "upload:u1:b.pdf")]


def test_search_failure_propagates_and_logs(qdrant, monkeypatch, caplog):
    async def boom(*a, **k):
        raise ConnectionError("qdrant down")

    async def go():
        await _seed(qdrant, [LC_POINT])
        monkeypatch.setattr(qdrant, "query_points", boom)
        return await rp.rag_search("q", collection=COLL)

    with caplog.at_level(logging.ERROR, logger="app.rag"), pytest.raises(ConnectionError):
        run(go())
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.rag"]
    assert events == [{"event": "rag_search_failed", "collection": COLL, "error": "ConnectionError"}]
    assert qdrant.closed  # 실패해도 클라이언트를 닫는다


def test_store_failure_propagates(qdrant, monkeypatch):
    async def boom(*a, **k):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(qdrant, "upsert", boom)
    with pytest.raises(ConnectionError):
        run(rp.store_chunks(["c"], {"source": "s"}, collection=COLL))


def test_connection_error_is_not_mistaken_for_missing_collection(qdrant, monkeypatch):
    """컬렉션 조회 실패(연결 오류)를 '없음'으로 보고 만들러 가지 않는다."""
    created = []

    async def down(*a, **k):
        raise ConnectionError("qdrant down")

    async def create(*a, **k):
        created.append(a)

    monkeypatch.setattr(qdrant, "get_collection", down)
    monkeypatch.setattr(qdrant, "collection_exists", down)
    monkeypatch.setattr(qdrant, "create_collection", create)
    with pytest.raises(ConnectionError):
        run(rp.rag_search("q", collection=COLL))
    assert created == []


# ── 크롤링 payload 키(text, 평평한 메타)와 LangChain 키(page_content, metadata.*) 불일치 ────────

# app/services/crawl.py _store_qdrant가 QDRANT_COLLECTION에 쓰는 모양
CRAWL_POINT = PointStruct(id=2, vector=vec("금리 인하 기대"), payload={
    "url": "https://example.com/n1", "title": "뉴스", "source": "github:o/r", "text": "금리 인하 기대",
    "chunk_index": 0})


def test_search_reads_crawl_layout(qdrant):
    async def go():
        await _seed(qdrant, [CRAWL_POINT, LC_POINT])
        return await rp.rag_search("금리 인하 기대", top_k=1, collection=COLL)

    hits = run(go())
    assert [(h["text"], h["title"], h["url"], h["source"]) for h in hits] == [
        ("금리 인하 기대", "뉴스", "https://example.com/n1", "github:o/r")]


def test_filter_source_matches_both_layouts(qdrant):
    async def go():
        await _seed(qdrant, [CRAWL_POINT, LC_POINT])
        lc = await rp.rag_search("x", top_k=5, collection=COLL, filter_source="upload:u1:a.pdf")
        crawl = await rp.rag_search("x", top_k=5, collection=COLL, filter_source="github:o/r")
        return [h["text"] for h in lc], [h["text"] for h in crawl]

    assert run(go()) == (["메모리 반도체"], ["금리 인하 기대"])


def test_delete_by_source_removes_stored_chunks(qdrant):
    """store_chunks로 올린 문서를 source로 지우면 벡터도 사라진다(예전 key='source' 필터는 metadata.source를 못 맞혔다)."""
    async def go():
        await rp.store_chunks(["c1", "c2"], {"source": "upload:u1:c.pdf"}, collection=COLL)
        await rp.store_chunks(["d1"], {"source": "upload:u1:d.pdf"}, collection=COLL)
        await rp.delete_chunks_by_source("upload:u1:c.pdf", collection=COLL)
        res = await qdrant.scroll(COLL, with_payload=True)
        return sorted(p.payload["page_content"] for p in res[0])

    assert run(go()) == ["d1"]
