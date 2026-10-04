# tests/chat/test_rag_owner_scope.py
"""업로드 문서는 올린 사용자 본인의 검색에만 쓰인다(DOCUMENT_COLLECTION == QDRANT_COLLECTION 공유 컬렉션).

- 업로드 점 payload에는 owner_user_id가 있고, 크롤링 점에는 없다(공용).
- rag_search는 '소유자 없음(공용) 또는 소유자 == 요청자'만 돌려준다. 요청자가 없으면 공용만.
- /api/chat·/api/chat/async·/api/graph/rag·/api/documents/search가 요청자 기준으로 검색한다.
DB·외부 호출 없이 돈다(메모리 Qdrant, 가짜 임베딩).
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from fastapi import FastAPI
from qdrant_client.http.models import (
    FieldCondition, Filter, IsEmptyCondition, MatchValue, PayloadField, PointStruct,
)

from app.services import rag_pipeline as rp
from tests.chat.conftest import vec

COLL = "owner_scope"
A, B = str(uuid.uuid4()), str(uuid.uuid4())


def test_visibility_filter_shape():
    """필터 구성: 소유자 없음 또는 소유자 == 요청자. 요청자가 없으면 소유자 없음만."""
    assert rp._visibility_filter(A) == Filter(should=[
        IsEmptyCondition(is_empty=PayloadField(key="owner_user_id")),
        FieldCondition(key="owner_user_id", match=MatchValue(value=A)),
    ])
    assert rp._visibility_filter(None) == Filter(must=[IsEmptyCondition(is_empty=PayloadField(key="owner_user_id"))])


def test_store_chunks_records_owner(qdrant):
    async def go():
        await rp.store_chunks(["A 메모"], {"source": "upload:a"}, collection=COLL, owner_user_id=A)
        pts, _ = await qdrant.scroll(COLL, with_payload=True)
        return [p.payload for p in pts]

    (payload,) = asyncio.run(go())
    assert payload["owner_user_id"] == A
    assert payload["page_content"] == "A 메모"


def _seed_mixed(qdrant):
    async def go():
        await rp.store_chunks(["A의 비공개 메모"], {"source": "upload:a"}, collection=COLL, owner_user_id=A)
        await rp.store_chunks(["B의 비공개 메모"], {"source": "upload:b"}, collection=COLL, owner_user_id=B)
        await qdrant.upsert(COLL, points=[PointStruct(id=7, vector=vec("공용 크롤링 메모"), payload={
            "url": "https://e/x", "title": "x", "source": "github:o/r", "text": "공용 크롤링 메모"})])
    asyncio.run(go())


def _texts(**kw) -> list[str]:
    return sorted(h["text"] for h in asyncio.run(rp.rag_search("비공개 메모", top_k=10, collection=COLL, **kw)))


def test_search_sees_public_and_own_only(qdrant):
    _seed_mixed(qdrant)
    assert _texts(viewer_user_id=A) == ["A의 비공개 메모", "공용 크롤링 메모"]
    assert _texts(viewer_user_id=B) == ["B의 비공개 메모", "공용 크롤링 메모"]
    assert _texts() == ["공용 크롤링 메모"]  # 요청자를 모르면 공용만
    # source를 지정해도 남의 업로드는 못 본다
    assert _texts(viewer_user_id=A, filter_source="upload:b") == []


# ── 라우트가 요청자 기준으로 검색하는지 ─────────────────────────────────────────

@pytest.fixture
def spy_search(monkeypatch):
    calls = []

    async def fake(query, top_k=5, collection=None, filter_source=None, viewer_user_id=None):
        calls.append(viewer_user_id)
        return []

    from app.routes import chat as chat_route
    from app.services import graph_rag
    monkeypatch.setattr(chat_route, "rag_search", fake)
    monkeypatch.setattr(graph_rag, "rag_search", fake)
    return calls


def _post(app, path, body, headers=None):
    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(path, json=body, headers=headers or {})
    return asyncio.run(call())


def test_chat_paths_search_as_requester(monkeypatch, spy_search):
    from app.database.postgres import get_pg_session
    from app.lib.jwt_auth import get_current_user_any
    from app.routes import chat as chat_route
    from app.tasks import agent_tasks

    async def fake_conv(db, user_id, cid):
        return "c0000000-0000-0000-0000-000000000001"

    async def fake_agent(*a, **k):
        return {"answer": "a", "steps": [], "citations": []}

    async def noop(*a, **k):
        return None

    class T:
        id = "t1"

    class Db:
        def add(self, o): pass
        async def execute(self, s):
            class R:
                def scalar_one_or_none(self): return None
            return R()
        async def commit(self): pass
        async def rollback(self): pass

    monkeypatch.setattr(chat_route, "_get_or_create_conversation", fake_conv)
    monkeypatch.setattr(chat_route, "run_agent", fake_agent)
    monkeypatch.setattr(chat_route, "set_active_conversation", noop)
    monkeypatch.setattr(chat_route, "get_llm_client", lambda: object())
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda **k: T())
    app = FastAPI()
    app.include_router(chat_route.router)
    app.dependency_overrides[get_current_user_any] = lambda: {"id": A, "client_id": "a"}
    app.dependency_overrides[get_pg_session] = lambda: Db()
    body = {"question": "q", "history": [{"role": "user", "content": "x"}]}
    assert _post(app, "/api/chat", body).status_code == 200
    assert _post(app, "/api/chat/async", body).status_code == 200
    assert spy_search == [A, A]


def test_graph_rag_requires_login_and_searches_as_requester(monkeypatch, spy_search):
    from app.lib.jwt_auth import get_current_user_any
    from app.routes import graph

    app = FastAPI()
    app.include_router(graph.router)
    assert _post(app, "/api/graph/rag", {"query": "삼성"}).status_code == 401  # 인증 없이는 못 쓴다
    assert spy_search == []

    app.dependency_overrides[get_current_user_any] = lambda: {"id": B}
    from app.services import graph_rag
    monkeypatch.setattr(graph_rag, "extract_mentioned_symbols", lambda text: [])
    res = _post(app, "/api/graph/rag", {"query": "삼성"})
    assert res.status_code == 200
    assert spy_search == [B]
