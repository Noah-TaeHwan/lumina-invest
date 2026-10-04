# tests/chat/test_chat_save_failure.py
"""/api/chat 저장 실패: 답변 응답 계약(200·본문)은 그대로 두고, 조용히 삼키지 않고 구조화 로그를 남긴다."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

import httpx
from fastapi import FastAPI

CID = "c0000000-0000-0000-0000-000000000001"
ANSWER = {"answer": "에이전트 답변", "steps": [], "citations": []}


class BrokenCommitDb:
    """add·execute는 되고 commit에서 DB 오류가 나는 세션."""

    def __init__(self):
        self.rolled_back = False

    def add(self, obj):
        pass

    async def execute(self, stmt):
        class R:
            def scalar_one_or_none(self):
                return None
        return R()

    async def commit(self):
        raise RuntimeError("db down: 질문 본문 같은 민감한 값")

    async def rollback(self):
        self.rolled_back = True


def _app(monkeypatch, db):
    from app.database.postgres import get_pg_session
    from app.lib.jwt_auth import get_current_user_any
    from app.routes import chat as chat_route

    async def fake_conv(db, user_id, cid):
        return CID

    async def fake_agent(*a, **k):
        return dict(ANSWER)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(chat_route, "_get_or_create_conversation", fake_conv)
    monkeypatch.setattr(chat_route, "run_agent", fake_agent)
    monkeypatch.setattr(chat_route, "set_active_conversation", noop)
    monkeypatch.setattr(chat_route, "get_llm_client", lambda: object())
    app = FastAPI()
    app.include_router(chat_route.router)
    app.dependency_overrides[get_current_user_any] = lambda: {"id": str(uuid.uuid4()), "client_id": "abc"}
    app.dependency_overrides[get_pg_session] = lambda: db
    return app


def test_chat_save_failure_keeps_response_and_logs(monkeypatch, caplog):
    db = BrokenCommitDb()
    app = _app(monkeypatch, db)

    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/api/chat", json={"question": "비밀 질문", "use_rag": False,
                                                   "history": [{"role": "user", "content": "x"}]})

    with caplog.at_level(logging.ERROR, logger="app.chat"):
        res = asyncio.run(call())
    assert res.status_code == 200
    assert res.json() == {**ANSWER, "conversation_id": CID}  # 기존 응답 계약 그대로
    assert db.rolled_back
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.chat"]
    assert events == [{"event": "chat_save_failed", "path": "sync", "conversation_id": CID,
                       "error": "RuntimeError"}]
    assert "비밀 질문" not in caplog.text and "민감한" not in caplog.text  # 질문·예외 메시지는 남기지 않는다
