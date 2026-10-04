# tests/chat/test_agent_task_save.py
"""Celery 경로(/api/chat/async → agent.run) 저장이 동기 경로처럼 client_id를 남기는지 본다."""
from __future__ import annotations

import asyncio
import uuid

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Chat, Conversation
from tests.evidence.p2_support import database, seed_user

ANSWER = {"answer": "에이전트 답변", "steps": [], "citations": []}


def _patch_worker(monkeypatch, url: str) -> None:
    """워커가 태스크 안에서 import하는 연결·LLM·에이전트를 테스트 DB와 가짜로 바꾼다."""
    import app.database.postgres as pgmod
    import app.lib.llm_client as llm
    import app.lib.redis_cache as rc
    import app.services.langgraph_agent as lg

    holder: dict = {}

    async def connect():
        holder["engine"] = create_async_engine(url)

    async def close():
        await holder.pop("engine").dispose()

    async def noop():
        return None

    async def fake_agent(db, ollama, model, question, history, rag_context=""):
        return dict(ANSWER)

    monkeypatch.setattr(pgmod, "connect_postgres", connect)
    monkeypatch.setattr(pgmod, "close_postgres", close)
    monkeypatch.setattr(pgmod, "get_session_factory",
                        lambda: async_sessionmaker(holder["engine"], expire_on_commit=False, class_=AsyncSession))
    monkeypatch.setattr(rc, "connect_redis", noop)
    monkeypatch.setattr(rc, "close_redis", noop)
    monkeypatch.setattr(llm, "get_llm_client", lambda: object())
    monkeypatch.setattr(lg, "run_agent", fake_agent)


def test_worker_saves_client_id(pg, monkeypatch):
    from app.tasks.agent_tasks import run_agent_task

    async def seed():
        async with database(pg) as factory:
            return await seed_user(factory, with_chat=False)

    who = asyncio.run(seed())
    _patch_worker(monkeypatch, pg)
    out = run_agent_task.run(user_id=who["user"]["id"], conversation_id=who["conversation_id"], question="질문",
                             history=[], llm_model="m", client_id=who["user"]["client_id"])
    assert out["answer"] == ANSWER["answer"]

    async def read():
        async with database(pg) as factory, factory() as db:
            chats = (await db.execute(select(Chat))).scalars().all()
            conv = await db.get(Conversation, uuid.UUID(who["conversation_id"]))
            return [(c.client_id, c.question) for c in chats], conv.message_count

    chats, count = asyncio.run(read())
    assert chats == [(who["user"]["client_id"], "질문")]
    assert count == 1


def test_chat_async_passes_client_id(monkeypatch):
    """라우트는 로그인 사용자의 client_id를 태스크 인자로 넘긴다."""
    from app.database.postgres import get_pg_session
    from app.lib.jwt_auth import get_current_user_any
    from app.routes import chat as chat_route
    from app.tasks import agent_tasks

    sent = {}

    class FakeTask:
        id = "t1"

    def fake_delay(**kwargs):
        sent.update(kwargs)
        return FakeTask()

    async def fake_conv(db, user_id, cid):
        return "c0000000-0000-0000-0000-000000000001"

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", fake_delay)
    monkeypatch.setattr(chat_route, "_get_or_create_conversation", fake_conv)
    app = FastAPI()
    app.include_router(chat_route.router)
    app.dependency_overrides[get_current_user_any] = lambda: {"id": str(uuid.uuid4()), "client_id": "abc123"}
    app.dependency_overrides[get_pg_session] = lambda: None

    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/api/chat/async", json={"question": "q", "history": [{"role": "user", "content": "x"}],
                                                         "use_rag": False})

    res = asyncio.run(call())
    assert res.status_code == 200
    assert sent.get("client_id") == "abc123"
