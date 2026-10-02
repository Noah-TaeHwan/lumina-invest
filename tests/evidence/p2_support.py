# tests/evidence/p2_support.py
"""저장·API(P2) 테스트 공용: DB 엔진, 사용자·스레드 시드, 가짜 검색·생성기·JEV, 테스트용 앱.

app.main은 import하지 않는다(Neo4j·LangGraph·Celery를 끌어온다). 필요한 라우터만 붙인 작은 앱을 만든다.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.lib import jev, jev_service
from app.models import Chat, Conversation, User
from app.services.evidence import runner as rn

CO, CORP = "삼성전자", "00126380"
ANSWER = "회사는 메모리 반도체를 생산한다. 2025년 영업이익은 32조 7,260억원이다. 문단에서 확인할 수 없습니다."
PASSAGES = [{"passage_id": f"{CORP}-II-{i}", "rcept_no": "20260312000123", "section": "II. 사업의 내용", "idx": i,
             "sha256": f"{i:064x}", "text": t}
            for i, t in enumerate(["회사는 메모리 반도체와 스마트폰을 생산한다.",
                                   "2025년 영업이익은 32조 7,260억원이다.",
                                   "주요 원재료는 웨이퍼다."])]


@asynccontextmanager
async def database(url: str):
    engine = create_async_engine(url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def seed_user(factory, *, with_chat: bool = True, answer: str = ANSWER) -> dict:
    """사용자 1명 + 스레드 1개(+ 메시지 1개)를 만들고 id를 돌려준다."""
    tag = uuid.uuid4().hex[:8]
    async with factory() as db:
        user = User(name=tag, email=f"{tag}@example.com", password_hash="x", client_id=tag)
        db.add(user)
        await db.flush()
        conv = Conversation(user_id=user.id, title="t", message_count=0)
        db.add(conv)
        await db.flush()
        out = {"user": {"id": str(user.id), "client_id": tag, "roles": ["user"]}, "conversation_id": str(conv.id)}
        if with_chat:
            chat = Chat(user_id=user.id, client_id=tag, conversation_id=conv.id, question="q", answer=answer,
                        steps=[], citations=[])
            db.add(chat)
            await db.flush()
            out["chat_id"] = str(chat.id)
        await db.commit()
    return out


class FakeLLM:
    """generate_answer가 부르는 chat()만 흉내 낸다."""

    def __init__(self, answer: str = ANSWER, exc: Exception | None = None):
        self.answer, self.exc = answer, exc
        self.calls: list[tuple[str, list[dict], dict]] = []

    async def chat(self, model, messages, options=None):
        self.calls.append((model, messages, options))
        if self.exc:
            raise self.exc
        return self.answer


def fake_search(passages=PASSAGES):
    calls: list[tuple[str, str]] = []

    async def search(corp_code: str, question: str) -> list[dict]:
        calls.append((corp_code, question))
        return [dict(p) for p in passages]

    search.calls = calls
    return search


class FakeJev:
    """모든 주장에 같은 확률을 돌려주는 ServiceJevClient 대역."""

    def __init__(self, s: float = 0.8, c: float = 0.05, delay: float = 0.0):
        self.s, self.c, self.delay = s, c, delay
        self.calls = 0

    async def ask(self, state, questions, *, user_id, log_ctx=None, usage=None):
        self.calls += 1
        await asyncio.sleep(self.delay)
        answers = {q: {"supports": self.s, "contradicts": self.c, "says_nothing": round(1 - self.s - self.c, 6)}
                   for q in questions}
        return jev_service.ServiceJevResult(jev.request_key(state, questions), True, answers, 1.0, 100, 1,
                                            None, None, 200)


class _Room:
    """한도가 늘 남는 Redis."""

    async def get(self, key):
        return None


def make_runner(client=None) -> rn.Runner:
    return rn.Runner(client or FakeJev(), jev_service.Quota(_Room()))


class Who:
    """요청 사용자를 테스트 중에 바꿀 수 있게 담아 둔다."""

    def __init__(self, user: dict | None = None):
        self.user = user


def make_app(factory, who: Who, *, search=None, llm=None, runner=None, companies=None, krx_search=None) -> FastAPI:
    from app.database.postgres import get_pg_session
    from app.lib.jwt_auth import get_current_user_any
    from app.lib.llm_client import get_llm_client
    from app.lib.session import get_current_user
    from app.routes import admin, conversations, evidence

    app = FastAPI()
    app.include_router(evidence.router)
    app.include_router(conversations.router)
    app.include_router(admin.router)

    async def session():
        async with factory() as db:
            yield db

    def current():
        if who.user is None:
            from fastapi import HTTPException
            raise HTTPException(401, "로그인이 필요합니다.")
        return who.user

    app.dependency_overrides[get_pg_session] = session
    app.dependency_overrides[get_current_user_any] = current
    app.dependency_overrides[get_current_user] = current
    app.dependency_overrides[evidence.get_session_factory] = lambda: factory
    app.dependency_overrides[evidence.get_passage_search] = lambda: search
    app.dependency_overrides[evidence.get_company_list] = lambda: companies
    if krx_search is not None:
        app.dependency_overrides[evidence.get_krx_search] = lambda: krx_search
    app.dependency_overrides[get_llm_client] = lambda: llm or FakeLLM()
    app.dependency_overrides[evidence.get_runner] = lambda: runner or make_runner()
    return app


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def drain() -> None:
    """요청이 띄운 판정 작업이 끝날 때까지 기다린다."""
    from app.services.evidence import background

    while background._TASKS:
        await asyncio.gather(*list(background._TASKS), return_exceptions=True)
