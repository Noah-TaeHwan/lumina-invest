# tests/chat/test_documents_scope.py
"""/api/documents/search는 로그인 사용자가 올린 문서만 검색한다.

RAG 검색이 늘 빈 결과였을 때(비동기 클라이언트 결함)는 드러나지 않았지만, 검색이 살아나면
다른 사용자가 올린 문서 청크가 보인다. 빈 PostgreSQL(EVIDENCE_TEST_DATABASE_URL)과 메모리 Qdrant로 본다.
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
from fastapi import FastAPI

from app.config import settings
from app.models import UploadedDoc
from app.services import rag_pipeline as rp
from tests.evidence.p2_support import database, seed_user


async def _upload(factory, who: dict, filename: str, chunks: list[str]) -> str:
    """업로드 라우트가 남기는 것과 같은 source_key로 벡터와 메타를 만든다."""
    key = f"upload:{who['user']['id']}:{filename}"
    await rp.store_chunks(chunks, {"url": f"upload://{filename}", "title": filename, "source": key},
                          collection=settings.DOCUMENT_COLLECTION)
    async with factory() as db:
        db.add(UploadedDoc(filename=filename, uploader="x", user_id=uuid.UUID(who["user"]["id"]), source_key=key,
                           chunks=len(chunks), file_size=1, ext=".txt"))
        await db.commit()
    return key


def test_document_search_is_scoped_to_uploader(pg, qdrant):
    from app.database.postgres import get_pg_session
    from app.lib.session import get_current_user
    from app.routes import documents

    async def go():
        async with database(pg) as factory:
            a = await seed_user(factory, with_chat=False)
            b = await seed_user(factory, with_chat=False)
            c = await seed_user(factory, with_chat=False)  # 올린 문서가 없다
            await _upload(factory, a, "a.txt", ["A의 비공개 메모"])
            key_b = await _upload(factory, b, "b.txt", ["B의 비공개 메모"])

            current = {}

            async def session():
                async with factory() as db:
                    yield db

            app = FastAPI()
            app.include_router(documents.router)
            app.dependency_overrides[get_current_user] = lambda: current["user"]
            app.dependency_overrides[get_pg_session] = session

            async def search(who, **body):
                current["user"] = {**who["user"], "email": "x@example.com"}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
                    res = await cl.post("/api/documents/search", json={"query": "비공개 메모", "top_k": 10, **body})
                assert res.status_code == 200
                return [h["text"] for h in res.json()["hits"]]

            return (await search(a), await search(a, source=key_b), await search(b, source=key_b),
                    await search(c))

    mine, other_source, own_source, none = asyncio.run(go())
    assert mine == ["A의 비공개 메모"]          # 남의 문서는 섞이지 않는다
    assert other_source == []                   # 남의 source_key를 지정해도 못 본다
    assert own_source == ["B의 비공개 메모"]
    assert none == []                           # 올린 문서가 없으면 빈 결과
