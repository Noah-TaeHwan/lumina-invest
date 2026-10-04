# tests/chat/test_vector_store_errors.py
"""벡터 저장소(Qdrant) 장애 시 응답: 문서 API는 503 "벡터 저장소 오류", GraphRAG는 그래프 결과를 살리고
vector_results만 비운다. 오류 본문에 예외 메시지(Qdrant URL 등)를 싣지 않는다. DB·외부 호출 없음."""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from fastapi import FastAPI

SECRET = "http://qdrant.internal:6333 refused"
UID = str(uuid.uuid4())


async def boom(*a, **k):
    raise ConnectionError(SECRET)


class FakeDb:
    """문서 라우트가 쓰는 만큼만: source_key 목록 조회, 문서 1건 조회, add·delete·commit 기록."""

    def __init__(self, doc=None, sources=()):
        self.doc, self.sources = doc, list(sources)
        self.added, self.deleted, self.commits = [], [], 0

    async def execute(self, stmt):
        db = self

        class R:
            def scalar_one_or_none(self):
                return db.doc

            def scalars(self):
                class S:
                    def all(self):
                        return db.sources
                return S()
        return R()

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def _docs_app(monkeypatch, db):
    from app.database.postgres import get_pg_session
    from app.lib.session import get_current_user
    from app.routes import documents

    monkeypatch.setattr(documents, "store_chunks", boom)
    monkeypatch.setattr(documents, "rag_search", boom)
    monkeypatch.setattr(documents, "delete_chunks_by_source", boom)

    async def parse(filename, content, ollama):
        return ["청크"]

    monkeypatch.setattr(documents, "parse_document", parse)
    monkeypatch.setattr(documents, "get_llm_client", lambda: object())
    app = FastAPI()
    app.include_router(documents.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": UID, "email": "u@example.com"}
    app.dependency_overrides[get_pg_session] = lambda: db
    return app


def _call(app, method, path, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.request(method, path, **kw)
    return asyncio.run(go())


def _assert_503(res):
    assert res.status_code == 503
    assert res.json() == {"detail": "벡터 저장소 오류"}
    assert "qdrant" not in res.text


def test_upload_vector_store_error_is_503_without_row(monkeypatch):
    db = FakeDb()
    res = _call(_docs_app(monkeypatch, db), "POST", "/api/documents/upload",
                files={"file": ("a.txt", b"hello world", "text/plain")})
    _assert_503(res)
    assert db.added == [] and db.commits == 0  # 0청크 문서 행을 만들지 않는다


def test_search_vector_store_error_is_503(monkeypatch):
    db = FakeDb(sources=[f"upload:{UID}:a.txt"])
    _assert_503(_call(_docs_app(monkeypatch, db), "POST", "/api/documents/search", json={"query": "q"}))


def test_delete_vector_store_error_is_503_and_keeps_row(monkeypatch):
    from app.models import UploadedDoc

    doc = UploadedDoc(id=uuid.uuid4(), filename="a.txt", uploader="u", user_id=uuid.UUID(UID),
                      source_key=f"upload:{UID}:a.txt", chunks=1, file_size=1, ext=".txt")
    db = FakeDb(doc=doc)
    _assert_503(_call(_docs_app(monkeypatch, db), "DELETE", f"/api/documents/{doc.id}"))
    assert db.deleted == [] and db.commits == 0  # 벡터를 못 지웠으면 메타도 남긴다(고아 벡터 방지)


# ── GraphRAG ──────────────────────────────────────────────────────────────────

def _graph_app():
    from app.lib.jwt_auth import get_current_user_any
    from app.routes import graph

    app = FastAPI()
    app.include_router(graph.router)
    app.dependency_overrides[get_current_user_any] = lambda: {"id": UID}
    return app


def test_graph_rag_keeps_graph_context_when_qdrant_fails(monkeypatch):
    from app.services import graph_rag

    async def related(sym):
        return {"found": True, "name": "삼성전자", "sector": "반도체", "competitors": [], "suppliers": [],
                "customers": [], "sector_peers": []}

    monkeypatch.setattr(graph_rag, "rag_search", boom)
    monkeypatch.setattr(graph_rag, "extract_mentioned_symbols", lambda text: ["005930"])
    monkeypatch.setattr(graph_rag, "get_related_stocks", related)
    res = _call(_graph_app(), "POST", "/api/graph/rag", json={"query": "삼성전자"})
    assert res.status_code == 200
    body = res.json()
    assert body["vector_results"] == []
    assert "005930" in body["graph_context"]
    assert "qdrant" not in res.text


def test_graph_rag_503_hides_exception_message(monkeypatch):
    from app.routes import graph

    async def fail(*a, **k):
        raise RuntimeError("bolt://neo4j.internal:7687 " + SECRET)

    monkeypatch.setattr(graph, "graph_rag_search", fail)
    res = _call(_graph_app(), "POST", "/api/graph/rag", json={"query": "q"})
    assert res.status_code == 503
    assert res.json() == {"detail": "GraphRAG 실패"}
